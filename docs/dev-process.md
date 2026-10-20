# Development process: two peer sessions and the controller

This is how the artel repository itself is built until the dogfood gate
(`docs/revision-2026-10-20.md` §2): two peer Claude Code sessions (`s1`,
`s2`) and the owner's controller session (`main`). Only those sessions read
this file. An artel agent (its environment has `ARTEL_TASK`) never follows
it: `dev/board.py` refuses to run there.

**Phases.** The plan's `**Active phase:** N` limits `claim` to tasks of that
phase or lower (`**Phase:** N` in a task section, default 1). Phase 2 tasks
are built by artel itself after the dogfood gate; peers never take them.

**Stalled cross-reviews.** When a merge waits for a cross-review and the
peer is busy with a task, the controller takes the review
(`claim --session main --review-only`) instead of letting the dependents
wait.

## The loop (DeLM-style)

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
6. Commit on `dev/T<id>`. **Right before integrating**: `tail` again and re-read
   `reviews/T<id>-*.md` — the controller may have added findings or a plan
   change (PLAN entry) while you worked; close them first. Run the suite with
   `PYTHONWARNDEFAULTENCODING=1 PYTHONWARNINGS=error::EncodingWarning` (CI sets
   them for child processes). Then
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

