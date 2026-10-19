# artel — specification

**Status:** draft 1, 2026-10-19 · **Package:** `artel-agents` (PyPI name `artel`
is reserved) · **Command:** `artel`

artel runs several coding agents in parallel on one git repository. The agents
are peers: there is no orchestrator. Each agent starts with a fresh context,
takes **one** task from a shared queue, does it in its own git worktree, and
exits. Agents share what they learn through a short append-only journal
instead of through one long conversation.

## 1. Why

- **Long sessions bloat.** Context grows with every file read and diff
  reviewed. Every later step pays for everything earlier, and usage quotas
  (e.g. a Claude Max plan) run out on context, not on work.
- **Orchestrators idle.** A main agent that delegates waits on its
  sub-agents, and they cannot build on each other until it relays results
  (DeLM, arXiv 2606.10662, calls these "relay bubbles").
- **Coordination by paper is expensive.** Hand-written journals, hand-off
  notes and merge checklists cost more than the code they coordinate.

Borrowed from DeLM (`github.com/yuzhenmao/DeLM`): first-claim-wins task queue,
append-only typed journal (FACT / FAIL), per-agent read cursor, length limits,
corrections as new entries. Not borrowed: n agents solving the same task,
copy-on-write workspaces, file exchange between agents, macOS-only storage.

## 2. Goals and non-goals

**Goals**
1. Fresh context per task: one task = one new agent process.
2. Peers, not a hierarchy: any agent can claim tasks and add tasks.
3. Safe parallelism on one machine: atomic claims, isolated worktrees,
   serialized heavy commands, serialized merges.
4. Cheap shared memory: a journal small enough to read at the start of every
   task (budgeted in characters).
5. Quota-aware: pause on usage limits instead of retrying into them; record
   per-task cost when the agent CLI reports it.
6. Project-agnostic: everything project-specific lives in `artel.toml` and a
   prompt template.
7. Zero dependencies: Python ≥ 3.11 standard library only.

**Non-goals (v0.1)**
- Several agents solving the same task; picking the best solution.
- A UI. The interface is the CLI and the journal.
- Windows hosts. Linux and macOS only.
- Agent CLIs other than Claude Code. The runner calls a configurable
  command, so others can be added later.
- Network services. artel never opens sockets.

## 3. Concepts

| Term | Meaning |
|---|---|
| **board** | SQLite database in the repository's common git dir: `$(git rev-parse --git-common-dir)/artel/board.db`. Shared by all worktrees, never committed. |
| **task** | Unit of work: title, body, area, priority, status. |
| **area** | Free-text tag naming what a task touches (`service`, `docs/spec`). Two running tasks never share an area. |
| **entry** | Journal record: kind, task, agent, text, basis. Append-only. |
| **agent** | One worker process: `claude -p` in one worktree for one task. Name `w<slot>-<task>`. |
| **runner** | `artel run`: keeps up to N agents alive, claims tasks for them, verifies and merges their work. Deterministic, uses no model tokens. |

### 3.1 Task states

```
open ──claim──▶ claimed ──agent READY──▶ verifying ──merged──▶ done
  ▲                │                         │
  │                ├──agent FAIL / timeout───┤
  └──release───────┴──lease expired──────────┘
claimed ──agent ASK──▶ blocked ──answer──▶ open
```

A claim carries a **lease** (default 3 h, renewed by the runner while the
agent process lives). An expired lease reopens the task with a FAIL entry,
so a crashed worker never keeps a task forever.

### 3.2 Journal entry kinds

| Kind | Written by | Meaning |
|---|---|---|
| `FACT` | agent | Established finding, with `basis` (file:line, command, commit). |
| `FAIL` | agent, runner | An approach that did not work, or why a task went back to the queue. |
| `ASK` | agent | Question for the human; blocks the task. |
| `ANSWER` | human | Answer to an `ASK`; reopens the task. |
| `READY` | agent | Work committed on the task branch; asks the runner to verify. |
| `DONE` | runner | Merged: merge commit SHA, cost if known. |
| `NOTE` | anyone | Anything else worth one line. |

Limits: `text` ≤ 400 characters, `basis` ≤ 200. Longer material goes into a
file in the repository and the entry references it. Entries are never edited;
a correction is a new entry that names the old entry's id.

## 4. Command line

```
artel init                         create board, write artel.toml template
artel add TITLE [--body B] [--area A] [--priority P]
artel claim [ID] [--agent NAME]    first claim wins; exit 3 and owner name if taken
artel release ID [--reason R]      give a claimed task back to the queue
artel post KIND ID TEXT [--basis B] [--agent NAME]
artel answer ASK_ENTRY_ID TEXT     human answer; reopens the task
artel tail [--agent NAME] [--all] [--budget CHARS]
artel status [--json]              tasks by state, running agents, pause state
artel export [--md FILE]           snapshot of tasks and journal as Markdown
artel run [-n N] [--once]          start the runner
artel pause [--until TIME] | artel resume
```

`tail` prints open tasks one line each, then entries after the agent's cursor,
newest last, cut to `--budget` characters (oldest dropped first); it advances
the cursor. `--all` ignores the cursor.

All mutations run inside `BEGIN IMMEDIATE` transactions; the database uses WAL.
A claim is a single `UPDATE … WHERE id=? AND status='open'` plus an area check
in the same transaction.

## 5. Runner

`artel run -n N` loops until stopped (or once with `--once`):

1. **Pause check.** If the board is paused (manually or by a usage limit),
   sleep until the pause ends.
2. **Claim.** For each free slot, claim the highest-priority open task whose
   area is not held by a running task. No claimable task → idle.
3. **Workspace.** `git worktree add <worktrees_dir>/<agent> -b artel/<task>
   <base>`; run `setup` from config (e.g. `npm ci`) if set.
4. **Prompt.** Render the prompt template with the task, `artel tail` output
   for this agent, config values, and the contract (§6).
5. **Agent.** Spawn the configured agent command in the worktree, e.g.
   `claude -p --model sonnet --permission-mode acceptEdits --output-format json`,
   with the prompt on stdin, a wall-clock `timeout`, and `ARTEL_TASK`,
   `ARTEL_AGENT`, `ARTEL_BOARD` in the environment.
6. **Outcome.** On exit, read the journal for this task:
   - `READY` → step 7;
   - `ASK` → task `blocked`, worktree kept;
   - `FAIL`, non-zero exit or timeout → task released with a runner `FAIL`
     entry (exit code, last 400 characters of output), worktree removed;
   - usage-limit signature in output (configurable regex) → task released
     without penalty, board paused until the reset time if the output names
     one, else for `limit_backoff` (doubling, capped).
7. **Verify.** Run `verify` from config in the worktree, wrapped in
   `heavy_lock` if set (e.g. a `flock` command). Red → task released with
   `FAIL` and the tail of the verify log.
8. **Merge.** Under the board's merge lock, merge `artel/<task>` into the
   integration target (§5.1) with `git merge --no-ff`. Conflict → abort,
   release with `FAIL`. Success → `DONE` with the merge SHA and the agent's
   reported cost/tokens if the JSON output has them; remove the worktree and
   the branch.

Heavy work runs one at a time; thinking runs in parallel. The default
`max_agents` is 2.

### 5.1 Integration target

A branch can be checked out in only one worktree, and the human's main
checkout usually holds the base branch. Two modes:

- **`checkout`** (default): merge inside the main checkout. The runner
  refuses to merge if that checkout is dirty or not on the base branch, and
  posts `FAIL` instead. Suits repositories where people work through artel.
- **`branch`**: merge into `artel/integration` in a dedicated worktree; the
  human merges that branch into the base when they choose.

## 6. Agent contract

The prompt template must tell the agent, and the default template does:

1. Work only inside the current worktree, on the current branch.
2. Read the task and the journal tail first. Do not redo anything a `FAIL`
   entry says failed.
3. Commit your changes on the branch. Do not merge, push or switch branches.
4. Before `READY`, run the project's checks for what you touched and have the
   change reviewed (the template names the reviewer, e.g. a sub-agent on a
   stronger model).
5. Record findings as `artel post FACT`, dead ends as `artel post FAIL`.
6. Need a human decision → `artel post ASK` and stop.
7. Finish with exactly one of `artel post READY` (one-line summary) or
   `artel post FAIL` (why), then exit.
8. Discovered other work → `artel add`, do not do it now.

## 7. Configuration — `artel.toml`

```toml
base = "main"
integration = "checkout"          # or "branch"
max_agents = 2
worktrees_dir = "../.artel-worktrees"
task_timeout = "90m"
lease = "3h"

[agent]
command = ["claude", "-p", "--model", "sonnet",
           "--permission-mode", "acceptEdits", "--output-format", "json"]
prompt = "artel/prompt.md"        # template; default built in
tail_budget = 6000                # characters of journal per prompt
limit_pattern = "(usage limit|rate limit)"
limit_backoff = "15m"

[commands]
setup = ""                        # e.g. "npm ci --prefer-offline"
verify = ""                       # e.g. "npm run build && npx vitest run"
heavy_lock = ""                   # e.g. "flock /tmp/heavy.lock"
```

## 8. Testing

- `unittest` only. The agent command is configurable, so tests use a fake
  agent script that writes journal entries and commits, or fails, or prints a
  usage-limit message.
- Real git repositories in temp dirs for worktree, verify and merge paths.
- Concurrency: several processes racing `claim` on one task; exactly one wins.

## 9. Open questions

1. **Claude Code's usage-limit output.** Exact text and exit code in `-p`
   mode are not documented here; the default `limit_pattern` must be
   confirmed on a real limit before v0.1.
2. **Permission mode for unattended agents.** `acceptEdits` still stops on
   shell commands not allowed in settings; the project must pre-allow its
   test and build commands, or choose `auto`. artel does not choose
   `bypassPermissions` by default.
3. **Cost fields** in `--output-format json` output: record whatever the CLI
   reports; do not compute prices.
