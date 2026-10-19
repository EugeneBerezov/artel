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
  contradiction → `python dev/board.py note SPEC-GAP <id> "<text>"`, follow
  the plan's provisional choice, mention it in the commit message.

**Read only what your task needs:** the plan's Global Constraints, your task
section (its line range is printed by `claim`), and the spec sections it
cites. Never read the whole spec or plan.

## How we build it: two peer sessions (DeLM-style)

Two persistent sessions, `s1` and `s2`, implement the plan in parallel. No
orchestrator: a shared queue and journal in `<git-common-dir>/artel-dev/`,
driven by `dev/board.py`. Your session name is given in the first message
(`You are s1`). Loop:

1. `python dev/board.py tail --session <S>` — read what your peer learned.
   Do not redo anything a FAIL entry says failed.
2. `python dev/board.py claim --session <S>` — take the next task.
   Nothing claimable → say so and wait for the owner.
3. `git worktree add .claude/worktrees/<S>-T<id> -b dev/T<id> main` and work
   only there. Never edit files in the main checkout: it belongs to
   `integrate`.
4. Implement with tests first (`unittest`). Run the tests you touched, then the
   whole suite: `python -m unittest discover -s tests -q`.
5. **Review by a fresh sub-agent** (Agent tool, model set explicitly):
   `sonnet`, or `opus` if `claim` said the task is risky. Give it the task
   section, the cited spec sections and `git diff main...HEAD`; it must not
   edit. Fix blocker/major findings; second round is a closure check only
   (were the findings closed, new blockers only). Still open after round 2 →
   ask the owner.
6. Commit on `dev/T<id>`, then
   `python dev/board.py integrate <id> --branch dev/T<id>` — merges into
   `main`, runs the suite, pushes (standing permission: `main` only, never
   force).
7. `python dev/board.py ci --sha <merge sha>` — the CI matrix (Linux, macOS,
   Windows). Red on another OS → fix it in a new branch as the same task.
8. `python dev/board.py note FACT|FAIL <id> "…" --session <S>` for anything
   your peer should know (≤ 400 bytes, with `--basis`). Remove the worktree.
   Next task.

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
