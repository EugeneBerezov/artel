# Revision 2026-10-20 — draft (for cross-attack)

Status: DRAFT. Owner decisions taken in a Telegram brainstorm on 2026-10-20
(06:33–06:50 UTC); this file turns them into a concrete change set for the
spec, the plan, the board and the dev process. Nothing here is applied yet.
Attack it for rationality before any edit.

## 0. Owner decisions this draft implements

| # | Question | Decision |
|---|---|---|
| Q1 | What is the revision for? | **B with elements of A**: cut v0.1 to the smallest artel that can build itself (dogfooding); take only cheap findings from the competitor research (`reports/01–03`). |
| Q2 | Attended or overnight? | **Overnight, simplified**: limit pauses, stop/signals and a Telegram notification on block/stop are in; vetting, writer rotation, stats, protected-paths behaviour are out. |
| Q3 | When does artel take over from the s1/s2 sessions? | **Immediately after the minimum.** The rest of the plan becomes artel tasks; artel lands work on `artel/integration`; the controller session reviews and merges into `main`. |
| Q4 | Which research findings go into the minimum? | **Two**: rerun a red verify once (flaky), guard the main checkout. Everything else goes to artel's queue. |
| Q5 | Benchmark ("стенд") for an article | **Both** a head-to-head (artel vs plain Claude Code, DeLM, Claude Code agent teams, Claude Squad; several models) and ablations inside artel (review on/off, merged-tree verify on/off, models). Staged, not all at once; built after dogfooding. |
| Q6 | How to restructure the plan | **Phases in the plan** (`Phase: 1|2`), board hands out phase 1 only; phase 2 tasks are later added to artel. |

## 1. Why this cut

- 16 of 32 tasks are done, yet artel has never carried one real task end to
  end (T16 is still ahead). The biggest risk is not a missing feature but an
  untested whole.
- Every task artel carries itself is a live test of review, verify, merge,
  limit pauses — and produces the journal data the benchmark needs.
- The research found no surveyed tool that verifies on the merged tree and
  merges unattended (reports/01); that is artel's distinctive part and it is
  already built (T13–T15). Vetting, rotation, stats and protected paths are
  refinements of it.
- The research also found no controlled evidence that a fresh reviewer beats
  self-review for code (reports/02). The ablation stage of the benchmark is
  where artel tests its own premise.

## 2. Phase 1 — the minimum (built by s1/s2 as today)

| Task | What | Why in the minimum | Deps (new) |
|---|---|---|---|
| T15 (reopened) | Runner core: close `reviews/T15-2.md` (major + minors 3, 6) | the slice itself | 13, 14 |
| T30 | Owner defaults (`slots_dir`, `fix_resumes_writer`, `autoapprove`, `protected_paths` setting, `--review 0`) | T16 must run with the new slot path | 13, 14 |
| T16 (controller) | Live slice check, one haiku task; plus `.artel/slots` permission check and PD-40 observation | first real end-to-end; gate for the rest | 15, 30 |
| T17 | Notifier (Telegram send-only, command notifier) | the owner learns where the night stopped | 16 |
| T18 | Requests: `answer/approve/reject/accept/rework/pause/resume/stop` | without them a blocked task cannot be unblocked even in the morning | 16 |
| T19 | `status`, `show`, `logs` | the human must see the board | 16 |
| T22 | Limits, disk guard, pauses, stop/signals, full reconcile, notifier wiring, log retention | overnight on a Max quota is impossible without limit pauses | 17, 18 |
| T23 | `artel init` | setting artel up in its own repo (and in benchmark repos) | 17 |
| **T33 (new)** | Night safety: verify rerun on red + main-checkout guard (§3 below) | the two cheap research findings | 22 |
| T24 (trimmed) | Runner loop e2e — every §13 loop through the real runner, **without the vet loops** (they move to T21) | the loops are only unit-tested otherwise; overnight trust needs them | 22, 23, 33 |

Phase 1 waves: W6 = T15, T30 → T16 · W7 = T17, T18, T19 · W8 = T22, T23 ·
W9 = T33 · W10 = T24. Shared-file chains are re-derived: `runner.py`
15 → 18 → 22 → 33; `integrate.py` 14 → 33 (→ 32 in phase 2); `stages.py`
13 → 15 (→ 31 → 32 → 21 → 26 in phase 2); `config_schema.py` 5 → 30 → 33.

**Gate to phase 2 ("dogfood gate")** — all of:
1. Phase 1 tasks done, cross-reviewed, CI green on all six jobs.
2. The controller runs `artel init` + `artel run` on a scratch clone of this
   repository with one small real task (haiku, owner's consent), and it
   reaches `done` on `artel/integration`; one overnight-like run with a
   forced limit pause (fake or real) resumes correctly.
3. The repository's `CLAUDE.md` is split (§5 below) so that artel's own
   agents do not follow the s1/s2 board loop.

## 3. New behaviour in the minimum (spec changes)

### 3.1 Verify rerun on red (T33)

- New setting `verify_reruns` (int, default 1, min 0).
- When the verify commands fail on the merged tree, the runner reruns the
  **whole** verify once on the **same** merged tree (same commit, same slot,
  fresh `git clean`). Green on the rerun → treated as green, a runner FACT
  entry `flaky verify: <command> red then green` is written, no attempt is
  counted, no fix stage. Red again → red as today (fix stage, counters).
- The rerun runs under the heavy lock like the first run; it does not rerun
  `setup`.
- Both runs are recorded (runs table or journal) with duration, so the
  benchmark can count flakes.
- Not done: quarantine lists, per-test reruns, bisection.

### 3.2 Main-checkout guard (T33)

- Agents run with `cwd` in a slot; Claude Code's `acceptEdits` already denies
  edits outside it, but `Bash` can still `cd` out.
- Before each agent stage the runner records the main worktree's `HEAD`,
  current branch and the list of refs under `refs/heads/` except `artel/*`
  (what the agent must never move). After the stage it compares.
  A change → the stage's outcome stands, but the board pauses
  (`pause_reason = "checkout_guard"`, detail names the ref) and notifies;
  `resume` clears it.
- **Not** guarded: file edits in the main worktree's working tree — the human
  may be working there while artel runs; any check would produce false
  positives. The guard catches the damaging case (moved branch / HEAD), not
  every write.
- Open question for the attack: is a ref-only guard worth its cost, or does
  it give false comfort?

### 3.3 Scope (§14a) rewrite

v0.1 = phase 1. Phase 2 items are "v0.1.x, built by artel itself": vetting
(T21), writer rotation and its live check (T25, T26), `artel stats` (T20),
transitions follow-up (T31), protected paths behaviour (T32), README (T27),
and the research backlog (§4). PD-52 defaults (`.artel/slots`,
`fix_resumes_writer = false`, `autoapprove = "none"`) are written into §9 so
the spec matches the code (closes spec gap 31).

## 4. Phase 2 — artel builds artel

- The controller converts each phase 2 plan task into `artel add`:
  title = plan task title; body = "Implement docs/plan.md Task N (lines a–b);
  read only that section, Global Constraints and the PDs it cites";
  `--area` = the task's Files; dependencies from the plan; `--strong` for
  RISKY tasks; `--merge ask` for every task in the first week.
- Research backlog, added as artel tasks after the gate, ranked:
  1. review-outcome logging (did review find something verify/human later
     confirmed; per-stage verdict counts) — feeds the ablation;
  2. fail-before/pass-after check for tasks marked `bugfix`;
  3. reviewer may run probe tests (template + permission profile change);
  4. pre-merge hook point (`commands.premerge`);
  5. `.worktreeinclude`-style copy of ignored files into slots.
- artel never pushes and never touches `main`. The controller session merges
  `artel/integration` into `main` after its own independent review and the CI
  check, as it does for s1/s2 today.
- s1/s2 are retired after the gate; the controller keeps the board.py
  journal only for its own notes, or it is archived.

## 5. Dev-process changes

- `CLAUDE.md` is split: code rules, commits and owner rules stay;
  the s1/s2 board loop moves to `docs/dev-process.md` (read by s1/s2 and the
  controller only). Otherwise artel's agents, which read `CLAUDE.md`, would
  try to `claim` tasks from `dev/board.py`.
- `dev/board.py`: tasks get `phase` (parsed from a `**Phase:** N` line,
  default 1); `claim` hands out only `phase <= board.max_phase` (default 1);
  `status` shows the phase. ~20 lines + tests.
- `reports/` (git-ignored) holds research reports; the benchmark design will
  live in `docs/bench.md` (tracked).

## 6. Benchmark ("стенд") — staged, after the gate

Not product behaviour; a separate document `docs/bench.md` written after the
gate. Outline:

- **Task set:** an existing benchmark with hidden tests (SWE-bench Verified or
  a lighter subset), plus a small multi-task repo where tasks interact
  (parallelism matters only there).
- **Stage 1 — head-to-head:** artel vs one long Claude Code session vs DeLM
  vs Claude Code agent teams vs Claude Squad, same tasks, same model.
  Metrics: tasks resolved (hidden tests), defects after merge, quota used per
  resolved task, human interventions, wall time.
- **Stage 2 — ablations inside artel:** review on/off, fresh reviewer vs
  writer self-review, merged-tree verify vs branch verify, verify rerun on/off.
- **Stage 3 — models:** haiku / sonnet / opus per stage role.
- What must already be recorded by phase 1: per-run stage, model, tokens,
  duration, outcome, finding counts (the `runs` table has all but the finding
  counts per run; T33 adds verify durations and flake marks).
- Quota: the owner's Max 5x is finite; each stage gets an explicit budget and
  the owner's consent before it runs.

## 7. What this draft does NOT change

Architecture, the transition table, pinned interfaces of done tasks, the
two-session process until the gate, cross-review rules, CI.

## 8. Risks I see (the attack should add more)

1. T24 trimmed but still large; it may be the long pole of phase 1.
2. The dogfood gate's "overnight-like run" is under-specified.
3. artel working on its own repository: a bug that corrupts `artel/integration`
   or the board stops the builder of the fix. Mitigation: the controller can
   always fall back to s1/s2 on `main`.
4. The benchmark competes with dogfooding for the same Max quota.
5. Phase 2 tasks written for "one fresh Sonnet writer" may be too large for a
   haiku-default artel; `--strong` or splitting may be needed.
