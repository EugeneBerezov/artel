# artel

artel runs coding agents in parallel on one git repository: a deterministic
runner, a shared task queue and journal, fresh independent reviewers, verify
on the merged tree, auto-merge into an integration branch. Python ≥ 3.11
standard library only. Apache-2.0. Everything in this repository is in English.

## Sources

- `docs/spec.md` — what artel does. The authority for behavior.
- `docs/plan.md` — implementation tasks, waves, pinned interfaces, provisional
  decisions (PD-*) for spec gaps.
- `docs/spike-2026-10-19.md` — measured facts about `claude -p`.
- Do not edit `docs/spec.md` or `docs/plan.md` while implementing. A gap or a
  contradiction → `python3 dev/board.py note SPEC-GAP <id> "<text>"`, follow
  the plan's provisional choice, mention it in the commit message.

**Read only what your task needs:** the plan's Global Constraints, your task
section (its line range is printed by `claim`), and the spec sections it
cites. Never read the whole spec or plan.

## How we build it: two peer sessions (DeLM-style)

Two persistent sessions, `s1` and `s2`, implement the plan in parallel and
check each other. A third, the owner's main session (`main`), is the
controller. No orchestrator: a shared queue and journal in
`<git-common-dir>/artel-dev/`, driven by `dev/board.py`. Your session name is
given in the first message (`You are s1`). Loop:

1. `python3 dev/board.py tail --session <S>` — read what your peer and the
   controller learned. Do not redo anything a FAIL entry says failed.
2. `python3 dev/board.py claim --session <S>` — take the next job. It is
   either a **cross-review** (`kind: review`, see below; reviews come first,
   because dependents wait for them) or a task. Nothing claimable → say so
   and wait. A reopened task (REOPEN or REVIEW-FAIL in the journal): read
   every `<git-common-dir>/artel-dev/reviews/T<id>-*.md` first and close each
   blocker/major finding.
3. `git worktree add .claude/worktrees/<S>-T<id> -b dev/T<id> main` and work
   only there. Never edit files in the main checkout: it belongs to
   `integrate`.
4. Implement with tests first (`unittest`). Run the tests you touched, then the
   whole suite: `python3 -m unittest discover -s tests -q`. Tests must be able
   to fail: for every behavior, name the mutation of the code that the test
   catches.
5. **Self-review by a fresh sub-agent** (Agent tool, model set explicitly):
   `sonnet`, or `opus` if `claim` said the task is risky. Give it the task
   section, the cited spec sections and `git diff main...HEAD`; it must not
   edit. Fix blocker/major findings; second round is a closure check only.
   Still open after round 2 → SPEC-GAP note and ask the owner.
6. Commit on `dev/T<id>`, then
   `python3 dev/board.py integrate <id> --branch dev/T<id> --session <S>` — merges into
   `main`, runs the suite, pushes (standing permission: `main` only, never
   force). The task is now `review(...)`: dependents stay blocked until the
   other session passes the cross-review.
7. `python3 dev/board.py ci --sha <merge sha>` — the CI matrix (Linux, macOS,
   Windows). Wait for the result. Red → `python3 dev/board.py reopen <id>
   --reason "<job, test, cause>" --session <S>` and fix it as the same task.
8. `python3 dev/board.py note FACT|FAIL <id> "…" --session <S>` for anything
   your peer should know (≤ 400 bytes, with `--basis`). Remove the worktree.
   Next job.

**Cross-review (`kind: review`)** — you check the other session's merged
task; you never review your own. Do not edit code.
- Spawn a fresh adversarial sub-agent (`sonnet`; `opus` if risky) with the
  task's plan section (`plan:` lines), the spec sections it cites, the diff
  (`diff:` line), and this brief: find defects, not praise; check pinned
  names and signatures exactly; check Linux, macOS and Windows behavior
  (paths, encodings, processes, locks, signals, quoting); name concrete code
  mutations the tests would not catch; run the whole suite.
- Check CI for the merge sha yourself (`board.py ci --sha`); red is a fail.
- Write the findings (severity, file:line, failing scenario) to
  `<git-common-dir>/artel-dev/reviews/T<id>-<n>.md` (n = next free number).
- Any blocker/major, or red CI → `python3 dev/board.py review <id> --verdict
  fail --reason "<≤400 B summary; see reviews/T<id>-<n>.md>" --session <S>`
  (the task reopens). Otherwise `--verdict pass`; put the minors in the file
  and in a FACT note.
- Cannot finish a held review (context full, restart) → `python3
  dev/board.py release <id> --reason "…"` returns it to the pool. If your
  peer is gone, your merges wait for the controller's review.

**The controller (`main`)** checks CI on every merge and reopens red tasks,
attacks the plan and the spec with independent read-only agents, takes a
stalled cross-review (`claim --review-only`), and amends `docs/plan.md` when
the owner delegated it (then `board.py init --from docs/plan.md --refresh`;
a PLAN entry appears in the journal). After a PLAN entry, re-read your task
section from the line range `claim` prints. Peers still never edit the spec
or the plan.

Tasks marked controller (T16, T25: live checks with a real `claude`) are not
handed out by `claim`; the owner's main session runs them with the owner's
consent and marks them with `python3 dev/board.py done <id>`.

Your context grows across tasks; that is intended. When it gets heavy, tell
the owner to restart you — all state is in the board, the journal and git.

## Code rules

- Stdlib only; no network except the Telegram notifier and `dev/board.py ci`.
- Cross-platform (spec §12): OS-specific code only in `artel/platform.py`;
  `encoding="utf-8"` on every `open` and `subprocess` text call; `pathlib`;
  no `/tmp`, no symlinks, no executable-bit assumptions; command settings as
  lists where possible. Tests must pass on Windows too:
  `TemporaryDirectory(ignore_cleanup_errors=True)`, no POSIX-only calls
  outside platform code.
- Interfaces between tasks are pinned in the plan — keep names and
  signatures exactly; a needed change → SPEC-GAP note and ask the owner.
- Tests never call a real `claude`; use `tests/fake_agent.py`.
- Real `claude -p` runs only in the live-check tasks, only with the owner's
  consent, on `haiku`, as few as possible — the owner's quota (Claude Max 5x)
  is finite.

## Commits

English. Subject: `T<id>: <what>`. Body: why, how it was verified, decisions
taken without the owner. End with
`Co-Authored-By: Claude <Model> <noreply@anthropic.com>` for the model that
wrote it. Code and docs may share a commit here.

## The owner

Writes in Russian; answer in Russian, briefly. Ask only for decisions you
cannot take from the spec, the plan or a sensible default — several at once,
each with options and your recommendation. Report results with evidence
(test output, CI link), never "done" without it.
