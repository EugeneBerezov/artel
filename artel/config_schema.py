"""Every artel setting: key, kind, default, one-line meaning, cost, long explanation (spec §9, §10).

No other module hard-codes a value from this table; later code reads `cfg[key]`.
"""
from dataclasses import dataclass

from artel.errors import UsageError


@dataclass(frozen=True)
class Setting:
    key: str                 # dotted: "max_agents", "models.write", "commands.verify", "notify.on"
    kind: str                # int | fraction | str | bool | duration | size | str_list | enum | enum_list | command | argv
    default: object          # TOML-typed default as written to artel.toml
    meaning: str             # one line: what it controls
    cost: str                # one line: quota/cost effect ("" when none)
    explain: str             # long explanation and trade-offs
    choices: tuple[str, ...] = ()
    minimum: int | None = None   # int kind only; `fraction` is a decimal 0 < x <= 1


_ROTATION_NOTE = (" Takes effect only when writer session rotation is built (Task 26, which may be skipped"
                  " after Task 25); until then every writer is fresh.")
_PLACEHOLDERS = ("Placeholders: {model}, {schema} (the schema's inline JSON text), {unattended_notice},"
                 " {project_tools}, {project_read_tools}.")


_PROTECTED_PATHS = (
    ".claude/", "CLAUDE.md", "CLAUDE.local.md", ".mcp.json", "artel.toml", ".artel/", ".github/", ".gitlab-ci.yml",
    ".githooks/", ".husky/", ".pre-commit-config.yaml", ".gitattributes",
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
    "Pipfile.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock")
_TEST_PATHS = ("tests/", "test/", "__tests__/", "spec/", "test_*", "*_test.*", "*.test.*", "*.spec.*",
               "conftest.py", "pytest.ini", "tox.ini", "noxfile.py", "jest.config.*", "vitest.config.*")
_TEST_MARKERS = (r"^\s*@(unittest\.)?skip\b", r"\bmark\.skip\b", r"\bskip([Ii]f\(\s*True|Unless\(\s*False)\b",
                 r"\b(it|test|describe)\.skip\(", r"\bx(it|describe|test)\(", r"\.only\(", r"pytest\.mark\.xfail",
                 r"\bxfail\(", r"@(unittest\.)?expectedFailure", r"os\._exit\(")
_VERIFY_TEST_ID_REGEX = (r"^(\w+ \([\w.]+\))(?:\r?\n[^\r\n]*)? \.\.\. ok\r?$", r"^PASSED (\S+)")
_PROBE_PROFILE = ("-p", "--model", "{model}", "--output-format", "stream-json", "--verbose",
                  "--permission-prompts", "none", "--disable-slash-commands", "--strict-mcp-config",
                  "--permission-mode", "default", "--max-turns", "1", "--tools", "", "--disallowedTools",
                  "Bash Edit Write NotebookEdit Read Glob Grep WebFetch WebSearch Task TodoWrite")
_EDIT_TOOLS = "Bash,Edit,Write,Read,Glob,Grep"   # Task 44: no messaging, scheduling, sub-agent or web tools
_TOOLS_NOTE = (" --tools is an allowlist: a list here replaces the whole default, so a project that needs"
               " another tool restates the full --tools value. Write it as a TOML list: the comma form splits"
               " \"Bash,Edit,...\" into separate arguments.")


def _s(key, kind, default, meaning, explain, cost="", choices=(), minimum=None) -> Setting:
    return Setting(key, kind, default, meaning, cost, explain, tuple(choices), minimum)


SETTINGS: tuple[Setting, ...] = (
    _s("base", "str", "main", "Branch the integration branch starts from.",
       "artel/integration is created from this branch and fast-forwarded as tasks verify. Change it when your"
       " trunk is not called main."),
    _s("max_agents", "int", 2, "Agent processes running at the same time.",
       "Each running agent occupies one slot (a git worktree). More agents finish the queue sooner and burn"
       " the usage limit faster; tasks with overlapping areas never run together.",
       cost="Linear in the number of agents: N agents spend quota N times as fast.", minimum=1),
    _s("max_attempts", "int", 2, "How many consecutive failed runs of one stage are tolerated.",
       "Consecutive failed runs of one stage (a success resets the count, PD-49). A stage that fails without a"
       " result (crash, timeout, unparsable output) is retried; after this many failures the task becomes stuck.",
       cost="Each retry is a full extra agent run.", minimum=1),
    _s("review_rounds", "int", 2, "Review rounds before a task is blocked for a human.",
       "Round 1 is a full review, later rounds only check that findings were closed. When blocking findings"
       " remain after the last round the task is blocked. 0 behaves like 1: one review, blocking findings go"
       " to you; reviews cannot be turned off.",
       cost="Each round is one fresh reviewer run (plus a fix run when findings exist).", minimum=0),
    _s("max_verify_fixes", "int", 2, "Fix attempts after a red verify on the merged tree.",
       "When verification on the merged tree fails, the writer gets the failure and tries to fix it. After"
       " this many tries the task is blocked.",
       cost="Each attempt is one writer run plus one verify run.", minimum=0),
    _s("verify_reruns", "int", 1, "Extra runs of a red task verify before it counts as red.",
       "A red verify of a task runs again this many times; a green rerun means the test is flaky and the merge"
       " waits for `artel accept` (merge = ask). 0 = a red verify counts at once.",
       cost="Each rerun is one more verify run (no agent tokens).", minimum=0),
    _s("max_stage_runs", "int", 12, "Finished agent runs per task, since a human last moved it, before it is stuck.",
       "Bounds the total work one task can consume whatever the loop: when a task reaches this many agent runs"
       " it becomes stuck until you answer, rework or retry it.",
       cost="Upper bound on the agent runs one task can spend unattended.", minimum=1),
    _s("max_consecutive_failures", "int", 3, "Failed agent stages in a row (across at least two tasks) that pause the runner.",
       "When this many agent stages fail one after another and at least two different tasks are among them, the"
       " runner pauses (an environment fault, not one bad task) until `artel resume`.",
       cost="Stops a broken environment from burning one failed run per task.", minimum=2),
    _s("max_branch_resets", "int", 1, "How often a fix stage that used up its attempts may reset the branch.",
       "a fix stage that used up max_attempts may reset the branch and rewrite the task this often; then the"
       " task is stuck (PD-63).",
       cost="Each reset is a fresh writer run on the task from scratch.", minimum=0),
    _s("merge", "enum", "auto", "Whether verified tasks merge on their own or wait for `artel accept`.",
       "auto: a verified task is fast-forwarded into the integration branch. ask: it waits in"
       " awaiting_accept until you run `artel accept`.", choices=("auto", "ask")),
    _s("stage_timeout", "duration", "90m", "Wall-clock limit for one agent stage.",
       "An agent that runs longer is terminated (soft, then hard after kill_grace) and the stage counts as a"
       " failed attempt. Durations: 500ms, 30s, 90m, 2h, 1d.",
       cost="Upper bound on what one hung stage can consume."),
    _s("setup_timeout", "duration", "20m", "Wall-clock limit for commands.setup in a slot.",
       "Setup (dependency install, build) that runs longer is killed and the setup_failed pause starts."),
    _s("verify_timeout", "duration", "30m", "Wall-clock limit for commands.verify on the merged tree.",
       "Verification that runs longer is killed and counts as a red verify."),
    _s("kill_grace", "duration", "30s", "Wait between the soft stop and the hard kill of a process tree.",
       "artel asks a process tree to stop, waits this long, then kills what is left."),
    _s("context_budget", "int", 8000, "Journal text (tokens) put into an agent prompt.",
       "How much of the journal a stage prompt may carry: newest and most relevant entries first, older ones"
       " are dropped.", cost="Larger prompts cost input tokens on every run.", minimum=0),
    _s("limit_stop", "enum", "allowed_warning", "When to pause because the usage limit is near.",
       "allowed_warning: pause as soon as the rate-limit event reports a warning. rejected: keep going until"
       " the API rejects a request.",
       cost="rejected uses every last bit of quota but wastes the run that gets rejected.",
       choices=("allowed_warning", "rejected")),
    _s("quota_stop_five_hour", "fraction", 0.95, "Five-hour window utilization at which the runner pauses.",
       "A decimal above 0 and at most 1. The quota pause starts when the five-hour window reaches this share"
       " of the plan's limit and ends at its reset.",
       cost="Lower = more of the window stays free for you."),
    _s("quota_stop_seven_day", "fraction", 0.8, "Weekly window utilization at which the runner pauses.",
       "A decimal above 0 and at most 1. The quota pause starts when the weekly window reaches this share; the"
       " rest of the week stays for you.",
       cost="Lower = more of the week stays free for you."),
    _s("night_budget_seven_day", "fraction", 0.15, "Most weekly utilization artel may use in any 24 hours.",
       "A decimal above 0 and at most 1. When the weekly window grew by this much within 24 hours the quota"
       " pause starts, so an unattended night cannot spend the week.",
       cost="Lower = slower unattended progress, more quota kept."),
    _s("limit_retry", "duration", "30m", "Gap unit between probes while no reset time is known.",
       "When the limit gives no reset time, the gap before the k-th probe is k times this (30, 60, 90, 120"
       " minutes by default). A probe is one tiny model call on models.probe.",
       cost="Each probe is one one-turn call without tools."),
    _s("limit_probes", "int", 4, "Probes in any 24 hours before waiting for `artel resume`.",
       "After this many probes within 24 hours the pause is marked exhausted and a notification says `resume`"
       " is needed. 0 = never probe.",
       cost="Each probe is one one-turn call without tools.", minimum=0),
    _s("min_free_disk", "size", "3G", "Free disk space below which the runner pauses.",
       "Checked before a slot is used. Sizes: 500M, 3G, plain bytes."),
    _s("base_cache_ttl", "duration", "6h", "How long a green check of the base branch is trusted.",
       "A red task verify first checks whether artel/integration is red on its own; a green result is reused"
       " for this long (PD-68).", cost="A base check is one verify run (no agent tokens)."),
    _s("shadow_verify", "bool", True, "Verify the reviewed commit when a review finds a blocker or major.",
       "A shadow verify measures whether verify alone would have caught what review found (PD-69). It runs in"
       " an idle slot and uses no agent tokens.", cost="One verify run per blocking review."),
    _s("slots_dir", "str", ".artel/slots", "Directory (relative to the repository) holding slots.",
       "Slots are git worktrees named artel-<n> under this directory, never inside .git. The default is kept"
       " apart from `.claude/worktrees`, where Claude Code and people keep their own worktrees."),
    _s("slot_keep", "str_list", ["node_modules", ".venv", "venv"], "Setup output kept when a slot is cleaned.",
       "After each stage `git clean -ffdx` runs in the slot except for these paths, so installed"
       " dependencies survive. Setup output outside this list makes the next stage run setup again (PD-73)."),
    _s("log_keep_tasks", "int", 50, "How many finished tasks keep their agent logs.",
       "Older logs are deleted; board rows stay.", minimum=0),
    _s("poll_interval", "duration", "5s", "How often the runner looks for work.",
       "The runner loop sleeps this long between ticks when nothing is ready."),
    _s("setup_retry", "duration", "15m", "How long the setup_failed pause lasts before setup is retried.",
       "The pause also ends when the setup command or commands.setup_inputs change, and at once with"
       " `artel resume --force`."),
    _s("autoapprove", "enum", "none", "Which vetted tasks start without human approval.",
       "Agent-proposed tasks are vetted first. none: a human approves every task (the v0.1 default: agent-proposed"
       " tasks wait for `artel approve`). non_risky: tasks without risk signals start by themselves. all: every"
       " vetted task starts.",
       cost="Every approved task costs agent runs; all can multiply the queue unattended.",
       choices=("none", "non_risky", "all")),
    _s("protected_paths", "str_list", list(_PROTECTED_PATHS),
       "Paths whose change forces merge=ask and a strong-model review.",
       "A path matches case-insensitively on `/`-separated names. An entry without an inner `/` (a trailing `/`"
       " is allowed) matches any path with a component equal to it (`CLAUDE.md` matches `docs/CLAUDE.md`,"
       " `.claude/` matches `.claude/settings.json`); an entry with an inner `/` is a root-anchored prefix"
       " (`scripts/verify.sh`, `tools/ci/`). templates_dir is matched like an entry of this list. Derived"
       " entries are added as exact paths: every commands.setup_inputs entry and files named by list-form"
       " setup/verify commands (string-form commands are not parsed: list their scripts here). An empty list"
       " disables this list and templates_dir; the derived entries stay.",
       cost="A strong-model review for tasks that touch them."),
    _s("test_paths", "str_list", list(_TEST_PATHS), "Paths that are tests or test configuration.",
       "Changes to these paths are checked by the test guard: a deleted test, fewer assertions or an added"
       " skip marker makes the merge wait for `artel accept`. Entries with `*` or `?` match file names;"
       " others match like protected_paths. Only pure test files and pure test configuration belong here:"
       " manifests such as package.json or pyproject.toml also change for dependencies, so they are left"
       " out; add them when the project's test configuration lives there."),
    _s("test_markers", "str_list", list(_TEST_MARKERS), "Regexes that mark an added test line as disabling a test.",
       "Each entry is a regular expression tried on every added line of a test_paths file; a match makes the"
       " merge wait for `artel accept`. The defaults are unconditional skips (`@skip` with or without"
       " arguments, `pytest.mark.skip` also as `pytestmark`, `skipIf(True`, `skipUnless(False`), `.only(`,"
       " xfail and `os._exit(`; conditional skips and skip calls in a test body (`skipTest`,"
       " `pytest.skip`) are not markers. Every entry must compile."),
    _s("test_assert_markers", "str_list", ["assert", "expect("], "Substrings that count a line as an assertion.",
       "Case-sensitive. A test file that loses more lines containing one of these than it gains makes the"
       " merge wait for `artel accept`; comment lines count on neither side."),
    _s("verify_test_id_regex", "str_list", list(_VERIFY_TEST_ID_REGEX), "Regexes that read passed test ids from verify output.",
       "Each entry is a regular expression compiled with re.MULTILINE and with exactly one group: the id of a"
       " passed test. The verify command must print test ids, e.g. `python -m unittest -v -b` (`-b` keeps a"
       " test's own output from splitting its `ok` line, which would read as a test that did not pass) or"
       " `pytest -rA`."
       " A task whose verify passes fewer of the base branch's tests makes the merge wait for `artel accept`."),
    _s("risk_signals", "str_list",
       ["deletes data", "quotas or money", "protocol or public contract", "visible to end users"],
       "Phrases the vetting agent looks for to mark a task risky.",
       "A task showing any of these signals needs human approval unless autoapprove = \"all\"."),
    _s("rotation.writer_max_tasks", "int", 1, "Tasks one writer session handles before a fresh one starts.",
       "1 = a fresh writer for every task." + _ROTATION_NOTE, minimum=1),
    _s("rotation.writer_max_context", "int", 0, "Writer context size (tokens) that forces a fresh session.",
       "0 = no threshold; measure first with `artel stats`." + _ROTATION_NOTE, minimum=0),
    _s("rotation.same_area_only", "bool", True, "Reuse a writer session only for tasks in the same area.",
       "When true a session is never carried over to a task with a different area." + _ROTATION_NOTE),
    _s("rotation.fix_resumes_writer", "bool", False, "Fix stages resume the writer's session.",
       "When true a fix after review or verify continues the writer's own session instead of starting a"
       " fresh one. Off until resume is measured (spike Q8, Task 25)." + _ROTATION_NOTE),
    _s("templates_dir", "str", ".artel/templates", "Directory (relative to the repository) with prompt templates.",
       "Templates found here override the built-in ones; `artel templates export` writes the built-ins."),
    _s("agent_command", "argv", "claude", "The agent executable.",
       "A string is one executable name or path and is never split; it is resolved with shutil.which, so"
       " claude.exe and claude.cmd work on Windows. Arguments need the list form."),
    _s("shell", "enum", "auto", "Shell used for string-form commands.",
       "auto: sh on POSIX, Git for Windows bash on Windows. List-form commands never use a shell and are the"
       " portable choice.", choices=("auto", "sh", "bash", "pwsh", "cmd")),
    _s("models.write", "str", "sonnet", "Model for writer and fix stages.",
       "Passed to the agent as --model.", cost="Larger models spend the usage limit faster."),
    _s("models.review", "str", "sonnet", "Model for reviewer, attack and vet stages.",
       "Passed to the agent as --model.", cost="Larger models spend the usage limit faster."),
    _s("models.strong", "str", "opus", "Model for strong tasks and large diffs.",
       "Used for tasks marked strong and for reviews of diffs over models.strong_diff_lines.",
       cost="The most expensive model; every use counts against the usage limit."),
    _s("models.probe", "str", "haiku", "Model for limit and agent_unavailable probes.",
       "Passed to the agent as --model for the one-turn probe call that tells whether the agent works again.",
       cost="One tiny call per probe."),
    _s("models.strong_diff_lines", "int", 300, "Diff size (lines) above which reviews use the strong model.",
       "Counts added plus removed lines against the integration branch.",
       cost="Lower = more reviews on the strong model.", minimum=0),
    _s("commands.setup", "command", "", "Command that prepares a slot (install dependencies).",
       "Empty = no setup. A string runs through the configured shell; a list runs directly (portable)."),
    _s("commands.setup_inputs", "str_list", [], "Files whose content changes re-run setup.",
       "Their content is hashed together with commands.setup; a changed hash means the slot is set up again."),
    _s("commands.verify", "command", "", "Command that verifies the merged tree (tests, build).",
       "Empty = nothing is verified and tasks go straight to merge. Exit code 0 is green."),
    _s("commands.heavy_lock", "command", "", "Command that takes a machine-wide lock for heavy work.",
       "Used by `artel heavy -- <command>` so only one agent runs a heavy job (full build, e2e) at a time."),
    _s("commands.heavy_lock_check", "command", "", "Command that tells whether the heavy lock is free.",
       "Exit code 0 = free. Empty = artel uses its own lock only."),
    _s("commands.project_tools", "str_list", [], "Extra tool patterns the writer may use.",
       "Substituted for {project_tools} in profiles.writer, e.g. \"Bash(npm test *)\". A pattern only grants"
       " permission: its tool must also be in the profile's --tools list."),
    _s("commands.project_read_tools", "str_list", [], "Extra read-only tool patterns the reviewer may use.",
       "Substituted for {project_read_tools} in profiles.reviewer. A pattern only grants permission: its tool"
       " must also be in the profile's --tools list."),
    _s("profiles.common", "str_list",
       ["-p", "--model", "{model}", "--output-format", "stream-json", "--verbose", "--json-schema", "{schema}",
        "--permission-prompts", "none", "--disable-slash-commands", "--strict-mcp-config",
        "--exclude-dynamic-system-prompt-sections", "--append-system-prompt-file", "{unattended_notice}",
        "--setting-sources", "project,local"],
       "Agent arguments shared by every stage.",
       "The prompt goes on stdin. " + _PLACEHOLDERS + " --setting-sources project,local keeps user-level"
       " settings (MCP servers, skills, hooks, plugins) out of unattended agents; the probe takes only this"
       " pair from here. A list here replaces the whole default; write it as a TOML list (the comma form splits"
       " \"project,local\").",
       cost="Changing these changes every agent run."),
    _s("profiles.writer", "str_list",
       ["--permission-mode", "acceptEdits", "--tools", _EDIT_TOOLS, "--allowedTools",
        "Bash(artel note *) Bash(artel add *) Bash(artel heavy *) Bash(git add *) Bash(git commit *)"
        " Bash(git status *) Bash(git diff *)", "{project_tools}"],
       "Extra arguments for writer and fix stages.",
       "Always passes --permission-mode explicitly. " + _PLACEHOLDERS + _TOOLS_NOTE,
       cost="Wider permissions let a writer do more per run, and more damage."),
    _s("profiles.reviewer", "str_list",
       ["--permission-mode", "acceptEdits", "--tools", _EDIT_TOOLS, "--allowedTools",
        "Bash(artel note *) Bash(artel heavy *) Bash(git diff *) Bash(git log *) Bash(git show *)"
        " Bash(git status *)", "{project_tools}", "{project_read_tools}", "--disallowedTools",
        "Bash(git * --output*)"],
       "Extra arguments for review stages: may write probe files and run project checks, never commits.",
       "acceptEdits lets a reviewer write probe files and run project checks through `artel heavy`; it has no"
       " git add or git commit. " + _PLACEHOLDERS + _TOOLS_NOTE,
       cost="Probes make reviews longer and catch defects green tests miss."),
    _s("profiles.attacker", "str_list",
       ["--permission-mode", "default", "--tools", "Bash,Read,Glob,Grep",
        "--disallowedTools", "Edit Write NotebookEdit Bash(git * --output*)",
        "--allowedTools",
        "Bash(artel note *) Bash(git diff *) Bash(git log *) Bash(git show *) Bash(git status *)",
        "{project_read_tools}"],
       "Extra arguments for vet and attack stages (read-only).",
       "Read-only: permission mode default, a --tools list without edit tools, plus a deny list for edit tools"
       " and `git --output` (a Bash pattern --tools cannot express). " + _PLACEHOLDERS + _TOOLS_NOTE,
       cost="Attackers that cannot read enough produce weaker reviews."),
    _s("profiles.probe", "str_list", list(_PROBE_PROFILE),
       "Arguments of the limit probe: one turn, no tools, no schema, no notice.",
       "Used instead of profiles.common for the probe call (PD-55). {model} is models.probe. Keep --max-turns 1"
       " and both fences, --tools \"\" (no tools at all) and the --disallowedTools list: a probe must cost almost"
       " nothing. Keep it a list: an empty item cannot be written in the comma form.",
       cost="One tiny call per probe."),
    _s("notify.on", "enum_list",
       ["blocked", "stuck", "awaiting_accept", "paused", "resumed", "runner_stopped", "error"],
       "Events that send a notification.",
       "Choose from blocked, stuck, awaiting_accept, paused, resumed, done, runner_stopped, error. error is an"
       " internal runner failure; its text names the task and stage, never a traceback.",
       choices=("blocked", "stuck", "awaiting_accept", "paused", "resumed", "done", "runner_stopped", "error")),
    _s("notify.command", "command", "", "Command that receives each notification text.",
       "Empty = no command notifier. The text arrives on stdin and in ARTEL_NOTIFY_TEXT."),
    _s("notify.telegram_enabled", "bool", False, "Send notifications to Telegram.",
       "Needs a bot token and a chat id (env variable or file). This is the only network traffic artel makes."),
    _s("notify.telegram_token_env", "str", "ARTEL_TELEGRAM_TOKEN", "Environment variable holding the bot token.",
       "Read when notify.telegram_token_file is empty or unreadable."),
    _s("notify.telegram_token_file", "str", "", "File holding the bot token.",
       "Preferred over the environment variable when set."),
    _s("notify.telegram_chat_env", "str", "ARTEL_TELEGRAM_CHAT", "Environment variable holding the chat id.",
       "Read when notify.telegram_chat_file is empty or unreadable."),
    _s("notify.telegram_chat_file", "str", "", "File holding the chat id.",
       "Preferred over the environment variable when set."),
    _s("notify.telegram_api_base", "str", "https://api.telegram.org", "Base URL of the Telegram Bot API.",
       "Change only for a self-hosted Bot API server or a test double."),
)

_BY_KEY = {s.key: s for s in SETTINGS}


def by_key(key: str) -> Setting:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise UsageError(f"unknown setting: {key}") from None


def toml_literal(value: object) -> str:
    """A TOML literal for a bool, int, float, str or list of those."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        out = ['"']
        for ch in value:
            if ch == "\\":
                out.append("\\\\")
            elif ch == '"':
                out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20 or ord(ch) == 0x7F:
                out.append("\\u%04x" % ord(ch))
            else:
                out.append(ch)
        out.append('"')
        return "".join(out)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_literal(v) for v in value) + "]"
    raise TypeError(f"cannot write {value!r} as TOML")


def defaults_markdown() -> str:
    rows = ["| key | default | meaning |", "|---|---|---|"]
    for s in SETTINGS:
        default = toml_literal(s.default).replace("|", "\\|")
        rows.append(f"| `{s.key}` | `{default}` | {s.meaning} |")
    return "\n".join(rows) + "\n"


if __name__ == "__main__":
    print(defaults_markdown())
