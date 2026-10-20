"""Every artel setting: key, kind, default, one-line meaning, cost, long explanation (spec §9, §10).

No other module hard-codes a value from this table; later code reads `cfg[key]`.
"""
from dataclasses import dataclass

from artel.errors import UsageError


@dataclass(frozen=True)
class Setting:
    key: str                 # dotted: "max_agents", "models.write", "commands.verify", "notify.on"
    kind: str                # int | str | bool | duration | size | str_list | enum | enum_list | command | argv
    default: object          # TOML-typed default as written to artel.toml
    meaning: str             # one line: what it controls
    cost: str                # one line: quota/cost effect ("" when none)
    explain: str             # long explanation and trade-offs
    choices: tuple[str, ...] = ()
    minimum: int | None = None   # int kind only


_ROTATION_NOTE = (" Takes effect only when writer session rotation is built (Task 26, which may be skipped"
                  " after Task 25); until then every writer is fresh.")
_PLACEHOLDERS = ("Placeholders: {model}, {schema} (the schema's inline JSON text), {unattended_notice},"
                 " {project_tools}, {project_read_tools}.")


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
    _s("max_attempts", "int", 2, "How many times a stage that crashed or timed out is retried.",
       "A stage that fails without a result (crash, timeout, unparsable output) is retried; after this many"
       " attempts the task becomes stuck.",
       cost="Each retry is a full extra agent run.", minimum=1),
    _s("review_rounds", "int", 2, "Review rounds before a task is blocked for a human.",
       "Round 1 is a full review, later rounds only check that findings were closed. When blocking findings"
       " remain after the last round the task is blocked.",
       cost="Each round is one fresh reviewer run (plus a fix run when findings exist).", minimum=0),
    _s("max_verify_fixes", "int", 2, "Fix attempts after a red verify on the merged tree.",
       "When verification on the merged tree fails, the writer gets the failure and tries to fix it. After"
       " this many tries the task is blocked.",
       cost="Each attempt is one writer run plus one verify run.", minimum=0),
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
    _s("min_free_disk", "size", "3G", "Free disk space below which the runner pauses.",
       "Checked before a slot is used. Sizes: 500M, 3G, plain bytes."),
    _s("slots_dir", "str", ".claude/worktrees", "Directory (relative to the repository) holding slots.",
       "Slots are git worktrees named artel-<n> under this directory, never inside .git."),
    _s("slot_keep", "str_list", ["node_modules"], "Paths kept when a slot is cleaned between stages.",
       "After each stage `git clean -ffdx` runs in the slot except for these paths, so installed"
       " dependencies survive."),
    _s("log_keep_tasks", "int", 50, "How many finished tasks keep their agent logs.",
       "Older logs are deleted; board rows stay.", minimum=0),
    _s("poll_interval", "duration", "5s", "How often the runner looks for work.",
       "The runner loop sleeps this long between ticks when nothing is ready."),
    _s("setup_retry", "duration", "15m", "How long the setup_failed pause lasts before setup is retried.",
       "The pause also ends when the setup command or commands.setup_inputs change, and at once with"
       " `artel resume --force`."),
    _s("autoapprove", "enum", "non_risky", "Which vetted tasks start without human approval.",
       "Agent-proposed tasks are vetted first. none: a human approves every task. non_risky: tasks without"
       " risk signals start by themselves. all: every vetted task starts.",
       cost="Every approved task costs agent runs; all can multiply the queue unattended.",
       choices=("none", "non_risky", "all")),
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
    _s("rotation.fix_resumes_writer", "bool", True, "Fix stages resume the writer's session.",
       "When true a fix after review or verify continues the writer's own session instead of starting a"
       " fresh one." + _ROTATION_NOTE),
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
       "Substituted for {project_tools} in profiles.writer, e.g. \"Bash(npm test *)\"."),
    _s("commands.project_read_tools", "str_list", [], "Extra read-only tool patterns the reviewer may use.",
       "Substituted for {project_read_tools} in profiles.reviewer."),
    _s("profiles.common", "str_list",
       ["-p", "--model", "{model}", "--output-format", "stream-json", "--verbose", "--json-schema", "{schema}",
        "--permission-prompts", "none", "--disable-slash-commands", "--strict-mcp-config",
        "--exclude-dynamic-system-prompt-sections", "--append-system-prompt-file", "{unattended_notice}"],
       "Agent arguments shared by every stage.",
       "The prompt goes on stdin. " + _PLACEHOLDERS + " --setting-sources is deliberately absent; add"
       " \"--setting-sources\", \"project,local\" to ignore user-level settings.",
       cost="Changing these changes every agent run."),
    _s("profiles.writer", "str_list",
       ["--permission-mode", "acceptEdits", "--allowedTools",
        "Bash(artel note *) Bash(artel add *) Bash(artel heavy *) Bash(git add *) Bash(git commit *)"
        " Bash(git status *) Bash(git diff *)", "{project_tools}"],
       "Extra arguments for writer and fix stages.",
       "Always passes --permission-mode explicitly. " + _PLACEHOLDERS,
       cost="Wider permissions let a writer do more per run, and more damage."),
    _s("profiles.reviewer", "str_list",
       ["--permission-mode", "default", "--disallowedTools", "Edit Write NotebookEdit", "--allowedTools",
        "Bash(artel note *) Bash(git diff *) Bash(git log *) Bash(git show *) Bash(git status *)",
        "{project_read_tools}"],
       "Extra arguments for reviewer, attack and vet stages.",
       "Read-only: permission mode default plus a deny list for edit tools. " + _PLACEHOLDERS,
       cost="Reviewers that cannot read enough produce weaker reviews."),
    _s("notify.on", "enum_list", ["blocked", "stuck", "paused", "resumed", "runner_stopped", "error"],
       "Events that send a notification.",
       "Choose from blocked, stuck, paused, resumed, done, runner_stopped, error. error is an internal runner"
       " failure; its text names the task and stage, never a traceback.",
       choices=("blocked", "stuck", "paused", "resumed", "done", "runner_stopped", "error")),
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
