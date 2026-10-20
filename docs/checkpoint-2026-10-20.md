# Checkpoint 2026-10-20 (controller)

The owner's main session (`main`, controller) wrote this checkpoint when the
owner stopped the overnight run of 2026-10-19/20. It records where the build
stands, what changed in the process, which decisions were taken without the
owner, and what is open. Raw review files and a journal snapshot are in
`docs/reviews/2026-10-20/`.

## Board

| Wave | Tasks | State |
|---|---|---|
| W1 | T1 | done |
| W2 | T2, T3, T4, T5, T28 (T2 follow-up) | done, cross-reviewed, CI green |
| W3 | T6, T7, T8, T9 | done, cross-reviewed, CI green |
| W4 | T10, T11, T12, T29 (T7 test hardening) | done, cross-reviewed, CI green |
| W5 | T13, T14 | done, cross-reviewed, CI green |
| W6 | T15 runner core + e2e slice | merged 7604e62 (05:27 UTC); **cross-review NOT done** (s2 stopped mid-review), CI at stop time pending |
| W6 | T16 live slice check | controller task, needs the owner's consent; blocks W7 |
| W7–W11 | T17–T27 | open |

16 of 29 tasks done (55 %). Every merged task has a green CI run on all six
jobs (Linux, macOS, Windows × Python 3.11, 3.13) and a passed cross-review.

## Process changes (on `main`)

- **Cross-review.** After `integrate`, a task is `review(...)`; dependents
  wait until a session other than the author passes it
  (`board.py review ID --verdict pass|fail`). A fail reopens the task. The
  controller takes stalled reviews (`claim --review-only`).
- **Board commands added:** `reopen` (done/stuck → open), `init --refresh`
  (re-read the plan, keep states), `review`, `release` of a held review, a
  fail cap for repeated review failures.
- **Review files** live in `<git-common-dir>/artel-dev/reviews/T<id>-<n>.md`
  (findings with severity, file:line, scenario). Copied here at checkpoint.
- **CLAUDE.md:** the loop now includes cross-review, red CI → reopen, "tests
  must name the mutation they catch", "right before integrate: tail and
  re-read the review files", and the CI encoding env
  (`PYTHONWARNDEFAULTENCODING=1 PYTHONWARNINGS=error::EncodingWarning`).
- **Independent attacks** on the spec and the plan (read-only opus agents):
  `docs/reviews/2026-10-20/spec-1.md`, `plan-1.md`.
- **CI minutes:** the private repo used 1,828 of 2,000 Actions minutes in one
  night (≈ 98 billable minutes per run: macOS ×10, Windows ×2). The owner made
  the repository public on 2026-10-20; standard runners are free for public
  repositories.

## Plan amendments made by the controller (owner to confirm)

All are tagged "(controller, 2026-10-20, owner to confirm)" in `docs/plan.md`.

- Attack #1 fixes: B1 (`"then"` quoted), B2/B3 (T10 signatures,
  `merge_identity_args` in slots), B4 and M1 (no double dispatch), M2–M10,
  S2–S7, accept loop; new PD-33 … PD-44; PD-6/22/23/26 amended.
- Task 28 (T2 follow-up) in W2, Task 29 (T7 test hardening) in W4.
- R17 also clears `review_closure`; R29b/R34a/R34b clear `accepted`; T27
  depends on 24, 25, 26; `review_rounds` uses `is None`.
- PD-45: integrate exactly the verified commit (meta `verified:<id>`).
- PD-46: a branch reset supersedes open review findings.
- `merge_committed`: the merged commit and the pre-fix tip must both be
  ancestors of HEAD.

## Open decisions for the owner (recommendation first)

1. Spec fundamentals (`spec-1.md` #1–#2): a branch under review can rewrite
   `.claude/settings.json`, `CLAUDE.md` and verify scripts → add
   `protected_paths` (forces `merge=ask` + strong review); a verify fix must
   get a closure review. Recommend yes to both.
2. Default `slots_dir` is under `.claude/worktrees`, shared with Claude Code's
   own worktrees (T10 now refuses to adopt foreign worktrees) → recommend
   `.artel/slots`.
3. PD-6: tasks in `blocked`/`awaiting_accept` hold no area (current) vs hold
   it. Recommend current.
4. `attempts` never resets on stage success → recommend reset on success.
5. R30 accepts any `stuck_from` (spec: vet/attack before code) → recommend
   restricting to the spec.
6. `--review 0`: the CLI rejects it, the config allows 0 → pick one rule.
7. PD-38 adds `error` to the default `notify.on` → recommend keep.
8. PD-40 assumes `api_error_status` 429 on a limit hit → verify in T16.
9. `fix_resumes_writer = true` rests on unmeasured spike Q8 → recommend
   `false` until T25.
10. `autoapprove` for agent-proposed tasks → recommend `none` in v0.1.

## Known open minors (not blocking)

Listed per task in the review files; the main ones: T4 read methods do not
map "database is locked" to `BoardBusy`; T10 runs `git worktree prune` on
every prepare (should be reconcile only), post-checkout hooks run in slots;
T14 does not detect a human rewind of `artel/integration`; T9 fake-agent
fidelity gaps.

## After the checkpoint (stop at ~05:30 UTC)

- T15 merged by s1 as 7604e62; the owner stopped the team while s2 was
  cross-reviewing it. The review was released unfinished (a partial
  `reviews/T15-1.md` may exist). **First job of the new team: the T15
  cross-review (opus, risky) and its CI** (`board.py ci --sha 7604e62…`).
- The repository is public since 2026-10-20; the T15 run is the first one
  after the switch — confirm in Settings → Billing that it consumed no
  included minutes.
- Leftover worktrees of the old s1 (`.claude/worktrees/s1-*`) hold no unmerged
  work except possibly WIP named in a journal NOTE; check before removing.

## How to resume

1. `python3 dev/board.py status` and `python3 dev/board.py tail --session <S> --all`.
2. Decide the open questions above; amend `docs/plan.md`, then
   `python3 dev/board.py init --from docs/plan.md --refresh`.
3. T16 (live slice, haiku, owner's consent) unblocks W7.
4. Restart s1/s2 with "You are s1/s2"; they follow `CLAUDE.md`.
