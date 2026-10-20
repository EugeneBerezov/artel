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
  contradiction → record it (peer sessions: `python3 dev/board.py note
  SPEC-GAP <id> "<text>"`; artel agents: `artel note`), follow the plan's
  provisional choice, mention it in the commit message.

**Read only what your task needs:** the plan's Global Constraints, your task
section (by its heading `### Task N:`), and the spec sections it cites. Never read the whole spec or plan.


## Who reads what

- Sessions `s1`, `s2` and the controller `main` follow
  `docs/dev-process.md` (board loop, cross-review, integrate, CI).
- An **artel agent** (a stage run by the artel runner; `ARTEL_TASK` is set)
  does only its stage: work in its slot, commit on its branch, report through
  its structured result and `artel note`. It never runs `dev/board.py`,
  never pushes, never merges and never edits `docs/spec.md` or
  `docs/plan.md`; the runner integrates and the controller merges into
  `main`. A spec gap goes into `artel note` and the result summary.

## Code rules

- Stdlib only; no network except the Telegram notifier and `dev/board.py ci`.
- Cross-platform (spec §12): OS-specific code only in `artel/platform.py`;
  `encoding="utf-8"` on every `open` and `subprocess` text call; `pathlib`;
  no `/tmp`, no symlinks, no executable-bit assumptions; command settings as
  lists where possible. Tests must pass on Windows too:
  `TemporaryDirectory(ignore_cleanup_errors=True)`, no POSIX-only calls
  outside platform code.
- Interfaces between tasks are pinned in the plan — keep names and
  signatures exactly; a needed change → a SPEC-GAP note (see Sources); the
  owner decides.
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
