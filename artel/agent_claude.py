"""Claude Code adapter: argv, stream-json parsing, result validation (spec §4–§6, §12).

This is the only module that knows stream-json field names. Facts come from
docs/spike-2026-10-19.md (Claude Code 2.1.304, model haiku, Linux):

- Q1  `--json-schema` works with `--output-format stream-json --verbose`; the parsed object is
      `result.structured_output` (`result` repeats it as a string); a missing `structured_output` or
      `is_error: true` is a failed run; `stop_reason` is "tool_use" on success and is no failure signal.
- Q2  one `rate_limit_event` per run, `rate_limit_info.{status, resetsAt, rateLimitType, unifiedWindows}`;
      only `status: "allowed"` was ever observed. Every other status is unobserved (PD-22): the parser
      tolerates any string and ranks it.
- Q3  `--append-system-prompt-file` works in `-p`.
- Q4  `result.permission_denials[]` holds `{tool_name, tool_use_id, tool_input}`; every profile passes
      `--permission-mode` explicitly (the owner's settings may say `auto`); the prompt goes on stdin only
      (variadic `--allowedTools` swallows a positional prompt).
- Q6  final `usage` is a SUM over all API calls of the run; per-call usage is `assistant.message.usage`.
- Q7  the notice is best effort; `--setting-sources` is in `profiles.common` since Task 44.
- PD-40 `api_error_status == 429` on an error result as the limit-hit shape is an ASSUMPTION (unobserved).
"""
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from artel import platform
from artel.errors import ArtelError

RESULT_TYPE = "result"                     # Q1
STRUCTURED_FIELD = "structured_output"     # Q1: parsed object; "result" repeats it as a string
DENIALS_FIELD = "permission_denials"       # Q4: [{tool_name, tool_use_id, tool_input}]
RATE_LIMIT_TYPE = "rate_limit_event"       # Q2
RATE_LIMIT_CONTAINER = "rate_limit_info"   # Q2: status, resetsAt (unix s), rateLimitType, unifiedWindows
API_ERROR_FIELD = "api_error_status"       # Q1: a result key; its value on a limit hit is unobserved
LIMIT_ERROR_STATUS = 429                   # PD-40 assumption (HTTP 429), to be checked by Task 16
ASSISTANT_TYPE = "assistant"               # Q6: assistant.message.usage is per API call
SYSTEM_TYPE = "system"                     # Q1: system/init carries session_id
IS_ERROR_FIELD = "is_error"                # Q1
SESSION_FIELD = "session_id"               # Q1: a result key and an init key
USAGE_FIELD = "usage"                      # Q1 (final, summed) / Q6 (assistant.message.usage, per call)
COST_FIELD = "total_cost_usd"              # Q6
TURNS_FIELD = "num_turns"                  # Q1
DURATION_FIELD = "duration_ms"             # Q1
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

SEVERITIES = ("blocker", "major", "minor")

RESULT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "outcome": {"enum": ["ready", "pass", "findings", "ask", "fail"]},
        "summary": {"type": "string"},
        "question": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {"severity": {"enum": list(SEVERITIES)}, "text": {"type": "string"},
                           "basis": {"type": "string"}},
            "required": ["severity", "text"]}},
        "closed": {"type": "array", "items": {"type": ["string", "integer"]}},
        "risky": {"type": "boolean"},                                    # vet only (PD-27)
        "risk_signals": {"type": "array", "items": {"type": "string"}},
        "duplicate_of": {"type": ["integer", "null"]},
    },
    "required": ["outcome", "summary"],
}

STAGE_OUTCOMES = {"vet": ("pass", "findings", "ask", "fail"),        # pass = adequate (PD-27)
                  "attack": ("pass", "findings", "ask", "fail"),
                  "write": ("ready", "ask", "fail"), "fix": ("ready", "ask", "fail"),
                  "review": ("pass", "findings", "ask", "fail")}


@dataclass(frozen=True)
class RateLimit:
    status: str                     # raw string; any value tolerated (Q2: only "allowed" observed)
    rank: int                       # PD-22: allowed 0, rejected 2, anything else 1
    resets_at: float | None         # unix seconds
    limit_type: str | None          # rateLimitType, e.g. "five_hour"
    utilization: float | None       # unifiedWindows[limit_type].utilization (0..1), if present
    windows: dict                   # unifiedWindows as given


def status_rank(status: str) -> int:
    if status == "allowed":
        return 0
    if status == "rejected":
        return 2
    return 1


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass
class Usage:                        # PD-23
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    first_call: dict | None = None  # first assistant.message.usage (startup cost, Q6)
    last_call: dict | None = None   # latest assistant.message.usage
    final: bool = False             # True when taken from the result record (a SUM over calls, Q6)

    @staticmethod
    def _context(call: dict | None) -> int | None:
        if call is None:
            return None
        return (_int(call.get("input_tokens")) + _int(call.get("cache_creation_input_tokens"))
                + _int(call.get("cache_read_input_tokens")))

    def context_tokens(self) -> int | None:
        """Context size for rotation (§4): input + cache tokens of the last call; None if no call was seen."""
        return self._context(self.last_call)

    def first_context_tokens(self) -> int | None:
        """The same measure for the first API call of the run (stored next to the last one, PD-23)."""
        return self._context(self.first_call)

    def to_dict(self) -> dict:
        """The token counters only; cost, turns and duration are separate board columns (PD-23)."""
        return {name: getattr(self, name) for name in TOKEN_FIELDS}


class StreamParser:
    def __init__(self) -> None:
        self.structured: dict | None = None
        self.usage = Usage()
        self.result_seen = False
        self.is_error = False
        self.api_error_status: object = None
        self.max_rank = 0
        self.assistant_seen = False         # any `assistant` record: the model was reached (PD-57)
        self.denials: list[dict] = []
        self.session_id: str | None = None
        self._calls: dict[str, dict] = {}   # message id → its last usage (one API call may emit several lines)
        self._lines_without_id = 0

    def limit_hit(self) -> bool:
        if self.max_rank == 2:
            return True
        return self.is_error and str(self.api_error_status) == str(LIMIT_ERROR_STATUS)

    def failed(self) -> bool:
        return (not self.result_seen) or self.is_error or self.structured is None

    def feed(self, line: str) -> RateLimit | None:
        try:
            obj = json.loads(line)
        except (ValueError, TypeError, RecursionError):
            return None
        if not isinstance(obj, dict):
            return None
        kind = obj.get("type")
        try:
            if kind == RATE_LIMIT_TYPE:
                return self._rate_limit(obj)
            if kind == ASSISTANT_TYPE:
                self._assistant(obj)
            elif kind == RESULT_TYPE:
                self._result(obj)
            elif kind == SYSTEM_TYPE and obj.get("subtype") == "init":
                self._session(obj)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None   # a malformed record of a known type is ignored like an unknown one
        return None

    def _session(self, obj: dict) -> None:
        session = obj.get(SESSION_FIELD)
        if isinstance(session, str) and session:
            self.session_id = session

    def _rate_limit(self, obj: dict) -> RateLimit | None:
        info = obj.get(RATE_LIMIT_CONTAINER)
        if not isinstance(info, dict):
            return None
        status = str(info.get("status", ""))
        limit_type = info.get("rateLimitType")
        windows = info.get("unifiedWindows")
        windows = dict(windows) if isinstance(windows, dict) else {}
        window = windows.get(limit_type) if isinstance(limit_type, str) else None
        utilization = window.get("utilization") if isinstance(window, dict) else None
        rank = status_rank(status)
        self.max_rank = max(self.max_rank, rank)   # first: a malformed optional field must not hide a rejection
        resets = info.get("resetsAt")
        try:
            resets_at = (float(resets) if isinstance(resets, (int, float)) and not isinstance(resets, bool)
                         else None)
        except OverflowError:
            resets_at = None
        if resets_at is not None and not math.isfinite(resets_at):
            resets_at = None
        event = RateLimit(
            status=status, rank=rank,
            resets_at=resets_at,
            limit_type=limit_type if isinstance(limit_type, str) else None,
            utilization=float(utilization) if isinstance(utilization, (int, float)) else None,
            windows=windows)
        return event

    def _assistant(self, obj: dict) -> None:
        self.assistant_seen = True
        self._session(obj)
        message = obj.get("message")
        usage = message.get(USAGE_FIELD) if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            return
        key = message.get("id")
        if not isinstance(key, str):
            self._lines_without_id += 1
            key = f"#line{self._lines_without_id}"
        self._calls[key] = usage   # the last usage seen for an id replaces the earlier one (PD-23)
        calls = list(self._calls.values())
        self.usage.first_call, self.usage.last_call = calls[0], self._calls[key]   # last = latest line seen
        if not self.usage.final:
            for name in TOKEN_FIELDS:
                setattr(self.usage, name, sum(_int(c.get(name)) for c in calls))

    def _result(self, obj: dict) -> None:
        self.result_seen = True
        self.is_error = bool(obj.get(IS_ERROR_FIELD))
        structured = obj.get(STRUCTURED_FIELD)
        self.structured = structured if isinstance(structured, dict) else None
        self.api_error_status = obj.get(API_ERROR_FIELD)
        denials = obj.get(DENIALS_FIELD)
        self.denials = [d for d in denials if isinstance(d, dict)] if isinstance(denials, list) else []
        self._session(obj)
        usage = obj.get(USAGE_FIELD)
        if isinstance(usage, dict):
            for name in TOKEN_FIELDS:
                setattr(self.usage, name, _int(usage.get(name)))
            self.usage.final = True
        cost = obj.get(COST_FIELD)
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.usage.cost_usd = float(cost)
        for field, attr in ((DURATION_FIELD, "duration_ms"), (TURNS_FIELD, "num_turns")):
            value = obj.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(self.usage, attr, value)


def is_unavailable(parser: StreamParser, exit_code: int | None) -> bool:
    """PD-57: the agent could not do its job for a reason that is not the task's. The caller has already ruled out
    a stop and a timeout; a rate-limited run (PD-40) is the limit's, never this. (a) no `assistant` record and no
    `result` with `is_error: false`: the CLI did not start, crashed or died before its first model call; (b) an error
    result with `api_error_status` 500-599 or 529."""
    if parser.limit_hit():
        return False
    if parser.is_error:
        try:
            status = int(parser.api_error_status)
        except (TypeError, ValueError):
            status = None
        if status is not None and (500 <= status <= 599 or status == 529):
            return True
    return not parser.assistant_seen and not (parser.result_seen and not parser.is_error)


def _closed_id(item) -> int | None:
    if isinstance(item, bool):
        return None
    if isinstance(item, int):
        return item if item > 0 else None
    if isinstance(item, str):
        m = re.fullmatch(r"\s*#?\s*([0-9]+)\s*", item)   # ASCII digits only
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
    return None


def validate_result(stage: str, obj: object) -> tuple[dict | None, str | None]:
    outcomes = STAGE_OUTCOMES.get(stage)
    if outcomes is None:
        return None, f"unknown stage {stage!r}"
    if not isinstance(obj, dict):
        return None, "result is not a JSON object"
    outcome = obj.get("outcome")
    if outcome not in outcomes:
        return None, f"outcome {outcome!r} is not allowed for stage {stage} (allowed: {', '.join(outcomes)})"
    summary = obj.get("summary")
    if not isinstance(summary, str):
        return None, "summary is required and must be a string"
    question = obj.get("question", "")
    if not isinstance(question, str):
        return None, "question must be a string"
    if outcome == "ask" and not question.strip():
        return None, "outcome ask needs a non-empty question"
    raw_findings = obj.get("findings", [])
    if not isinstance(raw_findings, list):
        return None, "findings must be a list"
    findings = []
    for i, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            return None, f"finding {i} is not an object"
        severity, text, basis = item.get("severity"), item.get("text"), item.get("basis", "")
        if severity not in SEVERITIES:
            return None, f"finding {i}: severity {severity!r} is not one of {', '.join(SEVERITIES)}"
        if not isinstance(text, str) or not text.strip():
            return None, f"finding {i}: text must be a non-empty string"
        if not isinstance(basis, str):
            return None, f"finding {i}: basis must be a string"
        findings.append({"severity": severity, "text": text, "basis": basis})
    if outcome == "findings":
        if stage == "vet":
            if not summary.strip():
                return None, "vet outcome findings needs a non-empty summary (the reason)"
        elif not findings:
            return None, "outcome findings needs a non-empty findings list"
    raw_closed = obj.get("closed", [])
    if not isinstance(raw_closed, list):
        return None, "closed must be a list"
    closed = []
    for item in raw_closed:
        number = _closed_id(item)
        if number is None:
            return None, f"closed item {item!r} is not a finding id"
        closed.append(number)
    result = {"outcome": outcome, "summary": summary, "question": question, "findings": findings, "closed": closed}
    if stage == "vet":
        risky = obj.get("risky", False)
        signals = obj.get("risk_signals", [])
        duplicate = obj.get("duplicate_of")
        if not isinstance(risky, bool):
            return None, "risky must be a boolean"
        if not isinstance(signals, list) or not all(isinstance(s, str) and s.strip() for s in signals):
            return None, "risk_signals must be a list of non-empty strings"
        if duplicate is not None and (isinstance(duplicate, bool) or not isinstance(duplicate, int)):
            return None, "duplicate_of must be an integer or null"
        result.update(risky=risky or bool(signals), risk_signals=list(signals), duplicate_of=duplicate)   # PD-27
    return result, None


def schema_json() -> str:
    """Compact, byte-identical across runs: part of the cached prefix (Q6)."""
    return json.dumps(RESULT_SCHEMA, separators=(",", ":"), ensure_ascii=True)


# Windows batch launchers (PD-41) ---------------------------------------------------------------------

# The two npm cmd-shim templates, line by line. Anything else in the file (env tweaks, another program,
# extra commands) makes it an unrecognized batch file: unwrapping would silently drop it (PD-41).
_NPM_BOILERPLATE = frozenset(line.lower() for line in (
    "@echo off", "goto start", ":find_dp0", "set dp0=%~dp0", "exit /b", ":start", "setlocal", "call :find_dp0",
    'if exist "%dp0%\\node.exe" (', 'set "_prog=%dp0%\\node.exe"', ") else (", 'set "_prog=node"',
    "set pathext=%pathext:;.js;=;%", ")",
    '@if exist "%~dp0\\node.exe" (', "@setlocal", "@set pathext=%pathext:;.js;=;%"))
_JS = r'"%(?:~dp0|dp0%)\\?(?P<js>[^"%\r\n]+?\.js)"\s+%\*'
_NPM_COMMANDS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r'^endlocal & goto #_undefined_# 2>nul \|\| title %comspec% & "%_prog%"\s+' + _JS + "$",
    r'^"%~dp0\\node\.exe"\s+' + _JS + "$",
    r"^node\s+" + _JS + "$"))
_WRAPPER_LINE = re.compile(r'^((?:"[^"\r\n]*"\s+)+)%\*\s*$')
_SENTINEL = "\0artel-command\0"


def _npm_targets(text: str) -> str | None:
    """The cli.js path when every line of `text` belongs to a known npm cmd-shim template, else None."""
    found = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower() in _NPM_BOILERPLATE:
            continue
        for pattern in _NPM_COMMANDS:
            m = pattern.match(line)
            if m:
                found.add(m.group("js"))
                break
        else:
            return None
    return found.pop() if len(found) == 1 else None


def unwrap_cmd_shim(text: str, shim_path: Path) -> list[str] | None:
    shim_path = Path(shim_path)
    targets = _npm_targets(text)
    if targets:
        parts = [p for p in re.split(r"[\\/]+", targets) if p]
        node = shim_path.parent / "node.exe"
        return [str(node) if node.is_file() else "node", str(shim_path.parent.joinpath(*parts))]
    return _unwrap_artel_wrapper(text)


def _unwrap_artel_wrapper(text: str) -> list[str] | None:
    """Only the exact script platform.write_wrapper writes: boilerplate lines plus one command line."""
    template = platform._cmd_script(_SENTINEL).splitlines()
    lines = text.splitlines()
    if len(lines) != len(template):
        return None
    command = None
    for line, expected in zip(lines, template):
        if expected == _SENTINEL:
            command = line
        elif line != expected:
            return None   # a PATH tweak, a cd or anything else would be silently dropped
    m = _WRAPPER_LINE.match(command or "")
    if m is None:
        return None
    tokens = re.findall(r'"([^"\r\n]*)"', m.group(1))
    if not tokens or any("%" in t.replace("%%", "") for t in tokens):
        return None   # a lone % is an environment expansion write_wrapper never emits
    return [t.replace("%%", "%") for t in tokens]


class CannotStartAgent(ArtelError):
    """agent_command cannot be turned into a runnable argv (not found, an unusable batch file): no agent can start
    (owner 2026-10-21: a stage is then `agent_unavailable`, a probe a failed probe)."""


def agent_argv(cfg) -> list[str]:
    command = cfg["agent_command"]
    try:
        argv = platform.resolve_argv([command] if isinstance(command, str) else list(command))   # PD-44
    except ArtelError as exc:
        raise CannotStartAgent(str(exc)) from exc
    if argv[0].lower().endswith((".cmd", ".bat")):
        path = Path(argv[0])
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CannotStartAgent(f"cannot read agent_command {path}: {exc}") from exc
        unwrapped = unwrap_cmd_shim(text, path)
        if unwrapped is None:
            raise CannotStartAgent(f"agent_command {path} is a batch file artel cannot unwrap; "
                                   "set agent_command to a list [node, cli.js] or to claude.exe")
        if unwrapped[0].lower().endswith((".cmd", ".bat")):   # the inline schema must never reach cmd.exe
            raise CannotStartAgent(f"agent_command {path} wraps another batch file ({unwrapped[0]}); "
                                   "set agent_command to a list [node, cli.js] or to claude.exe")
        argv = unwrapped + argv[1:]
    return argv


_PLACEHOLDER = re.compile(r"\{(model|schema|unattended_notice)\}")


def _has_permission_mode(tokens: list[str]) -> bool:
    for i, token in enumerate(tokens):
        if token.startswith("--permission-mode="):
            return len(token) > len("--permission-mode=")
        if token == "--permission-mode":
            return i + 1 < len(tokens) and bool(tokens[i + 1]) and not tokens[i + 1].startswith("-")
    return False


def build_argv(cfg, *, profile: str, model: str, notice_path: Path) -> list[str]:
    if profile not in ("writer", "reviewer", "attacker"):
        raise ValueError(f"unknown profile: {profile!r}")
    template = [*cfg["profiles.common"], *cfg[f"profiles.{profile}"]]
    if not _has_permission_mode(template):   # checked before substitution: a model name cannot satisfy it
        raise ArtelError(f"profiles.common + profiles.{profile} must pass --permission-mode explicitly")
    values = {"model": model, "schema": schema_json(), "unattended_notice": str(notice_path)}
    expanded: list[str] = []
    for token in template:
        if token == "{project_tools}":
            expanded.extend(cfg["commands.project_tools"])
        elif token == "{project_read_tools}":
            expanded.extend(cfg["commands.project_read_tools"])
        else:
            expanded.append(_PLACEHOLDER.sub(lambda m: values[m.group(1)], token))
    argv = agent_argv(cfg) + expanded
    if any("bypassPermissions" in t or t.startswith("--dangerously-skip-permissions") for t in argv):
        raise ArtelError("artel never runs an agent with bypassPermissions")   # agent_command arguments too
    return argv


# Sample lines in the shapes recorded by the spike (used by the fake agent and the tests) ---------------

_DEFAULT_USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0}
_FAKE_SESSION = "00000000-0000-4000-8000-000000000000"
_message_counter = [0]


def sample_init_line(session_id: str = _FAKE_SESSION) -> str:
    return json.dumps({"type": SYSTEM_TYPE, "subtype": "init", SESSION_FIELD: session_id, "model": "haiku",
                       "permissionMode": "default", "tools": ["Bash", "Read"]}, ensure_ascii=False)


def sample_result_line(structured: dict | None, usage: dict | None = None, *, is_error: bool = False,
                       denials: list[dict] | None = None, api_error_status: object = None) -> str:
    record = {
        "type": RESULT_TYPE, "subtype": "error_during_execution" if is_error else "success",
        IS_ERROR_FIELD: is_error, API_ERROR_FIELD: api_error_status, DURATION_FIELD: 1000, "duration_api_ms": 900,
        TURNS_FIELD: 2, "result": json.dumps(structured, ensure_ascii=False) if structured is not None else "",
        "stop_reason": "tool_use", SESSION_FIELD: _FAKE_SESSION, COST_FIELD: 0.001,
        USAGE_FIELD: dict(usage if usage is not None else _DEFAULT_USAGE),
        DENIALS_FIELD: list(denials or []), "terminal_reason": "completed",
    }
    if structured is not None:
        record[STRUCTURED_FIELD] = structured
    return json.dumps(record, ensure_ascii=False)


def sample_rate_limit_line(status: str, resets_at: int, limit_type: str, utilization: float) -> str:
    return json.dumps({
        "type": RATE_LIMIT_TYPE,
        RATE_LIMIT_CONTAINER: {
            "status": status, "resetsAt": resets_at, "rateLimitType": limit_type, "overageStatus": "rejected",
            "overageDisabledReason": "org_level_disabled", "isUsingOverage": False,
            "unifiedWindows": {limit_type: {"utilization": utilization, "resetsAt": resets_at}}},
        SESSION_FIELD: _FAKE_SESSION}, ensure_ascii=False)


def sample_assistant_line(text: str, usage: dict, *, message_id: str | None = None) -> str:
    if message_id is None:
        _message_counter[0] += 1
        message_id = f"msg_fake_{_message_counter[0]}"
    return json.dumps({
        "type": ASSISTANT_TYPE, SESSION_FIELD: _FAKE_SESSION,
        "message": {"id": message_id, "type": "message", "role": "assistant",
                    "content": [{"type": "text", "text": text}], USAGE_FIELD: dict(usage)}}, ensure_ascii=False)
