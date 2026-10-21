"""The limit probe (spec §5 step 5, PD-55): one tiny model call, no tools, no schema, no notice, that tells whether a
pause without a known end can lift. A probe never counts: no `runs` row, no attempt, no failure streak."""
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from artel import agent_claude, procs
from artel.agent_claude import StreamParser
from artel.gitutil import Repo
from artel.pauses import PROBE_WINDOW, PROBED_KINDS, Pause

PROMPT = "Reply with the single word OK."
PROBE_TIMEOUT = 120.0
_SETTING_SOURCES = "--setting-sources"
_THRESHOLDS = {"five_hour": "quota_stop_five_hour", "seven_day": "quota_stop_seven_day"}


@dataclass(frozen=True)
class ProbeResult:
    outcome: str                    # "ok" | "limit" | "unavailable"
    resets_at: float | None         # the limit's reset time, when the probe reported one
    log_path: Path
    windows: dict = field(default_factory=dict)   # {"five_hour": {"utilization": u, "resets_at": t}, ...}
    error: str | None = None        # the agent could not be started (not found, not runnable): why


def _setting_sources(tokens: Sequence[str]) -> list[str]:
    """The `--setting-sources` flag of a profile with its value (`--x v` or `--x=v`); [] when there is none."""
    for i, token in enumerate(tokens):
        if token == _SETTING_SOURCES:
            return list(tokens[i:i + 2])
        if token.startswith(_SETTING_SOURCES + "="):
            return [token]
    return []


def probe_argv(cfg) -> list[str]:
    """agent_argv + profiles.probe ({model} = models.probe) + the setting-sources pair of profiles.common, unless the
    probe profile sets its own. Never profiles.common itself: it carries the schema and the notice (PD-55)."""
    tokens = [t.replace("{model}", cfg["models.probe"]) for t in cfg["profiles.probe"]]
    if not _setting_sources(tokens):
        tokens += _setting_sources(cfg["profiles.common"])
    return agent_claude.agent_argv(cfg) + tokens


_STAMP = "%Y%m%dT%H%M%S%fZ"   # no ':' (Windows)


def log_time(path: Path) -> float | None:
    """When the probe whose log (or stderr log) this is started: the stamp in its name; None for another name."""
    try:
        return datetime.strptime(path.name.split("Z", 1)[0] + "Z", _STAMP).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _log_path(repo: Repo, now: float) -> Path:
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime(_STAMP)
    directory = repo.logs_dir / "probe"
    path = directory / f"{stamp}.jsonl"
    n = 1
    while path.exists():   # a frozen test clock, or two probes in one microsecond
        n += 1
        path = directory / f"{stamp}-{n}.jsonl"
    return path


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def event_windows(event) -> dict:
    """The usage windows a rate-limit event reports, PD-56 shape: {name: {"utilization": u, "resets_at": t | None}};
    the event's own window also takes the event's reset time."""
    out = {}
    for name, window in event.windows.items():
        if not isinstance(window, dict) or _number(window.get("utilization")) is None:
            continue
        out[name] = {"utilization": _number(window["utilization"]), "resets_at": _number(window.get("resetsAt"))}
    if event.limit_type and event.utilization is not None:
        own = out.get(event.limit_type, {}).get("resets_at")
        out[event.limit_type] = {"utilization": event.utilization,
                                 "resets_at": event.resets_at if event.resets_at is not None else own}
    return out


def run_probe(repo: Repo, cfg, env, *, now: float, stop_event: threading.Event | None = None,
              on_start: Callable[[int, int, str | None], None] | None = None) -> ProbeResult:
    """cwd is an empty directory of its own: no slot, no task, so the probe cannot touch the project. `on_start`
    records the probe's process like a stage's, so reconcile can end a probe a crashed runner left behind. An agent
    that cannot be started (not found, not runnable) is a failed probe (owner 2026-10-21)."""
    cwd = repo.artel_dir / "probe"
    cwd.mkdir(parents=True, exist_ok=True)
    log = _log_path(repo, now)
    parser = StreamParser()
    windows: dict = {}
    limit_reset: list[float | None] = []
    warned: list[bool] = []
    limit_rank = agent_claude.status_rank(cfg["limit_stop"])

    def on_line(line: str) -> None:
        event = parser.feed(line)
        if event is not None:
            windows.update(event_windows(event))
            if event.rank >= limit_rank:   # a warning counts when limit_stop says so: the pause must not lift into it
                limit_reset.append(event.resets_at)
                warned.append(True)

    try:
        res = procs.run_tree(probe_argv(cfg), cwd=cwd, env=env, stdin_text=PROMPT, timeout=PROBE_TIMEOUT,
                             kill_grace=cfg["kill_grace"], log_path=log, on_line=on_line, stop_event=stop_event,
                             on_start=on_start)
    except (procs.CannotStart, agent_claude.CannotStartAgent) as exc:
        return ProbeResult("unavailable", None, log, windows, error=str(exc))
    if parser.limit_hit() or warned:
        resets = next((r for r in reversed(limit_reset) if r is not None), None)
        return ProbeResult("limit", resets, log, windows)
    if res.timed_out or res.stopped or agent_claude.is_unavailable(parser, res.returncode):   # PD-55, PD-57
        return ProbeResult("unavailable", None, log, windows)
    return ProbeResult("ok", None, log, windows)


def due(pause: Pause, cfg, *, now: float, times: Sequence[tuple[float, str]]) -> bool:
    """The k-th probe of a pause with no known end is due `k * limit_retry` after the previous probe of this pause
    (its `since` for k = 1): a machine that wakes after hours runs one probe, not a burst (PD-55)."""
    if pause.kind not in PROBED_KINDS or pause.until is not None or pause.exhausted:
        return False
    if sum(1 for ts, _ in times if ts > now - PROBE_WINDOW) >= cfg["limit_probes"]:
        return False
    mine = [ts for ts, kind in times if kind == pause.kind and ts >= pause.since]
    anchor = max(mine) if mine else pause.since
    return now >= anchor + (len(mine) + 1) * cfg["limit_retry"]


def ended(pause: Pause, result: ProbeResult, cfg, *, now: float) -> bool:
    """`ok` ends the pause; a `quota` pause only when every window the probe saw is below its stop threshold."""
    if result.outcome != "ok":
        return False
    if pause.kind != "quota":
        return True
    seen = {name: w for name, w in result.windows.items() if name in _THRESHOLDS}
    # every window with an active quota cause must have been seen below its threshold; a probe that did not report
    # one of them proves nothing about it
    wanted = {name for name, key in _THRESHOLDS.items() if any(c.setting == key for c in pause.causes)} or set(seen)
    return bool(wanted) and all(name in seen and seen[name]["utilization"] < cfg[_THRESHOLDS[name]] for name in wanted)
