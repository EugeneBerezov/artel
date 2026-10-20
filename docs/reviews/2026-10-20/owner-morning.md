# For the owner — decisions to confirm (controller, night of 2026-10-19/20)

## Process changes (in CLAUDE.md / dev/board.py on main)
- s1 and s2 cross-review each other's merges; dependents wait for a pass; red CI -> reopen; review files in
  <git-common-dir>/artel-dev/reviews/. Controller takes stalled reviews (claim --review-only).
- board.py: reopen, init --refresh, review, release of a held review, review-fail cap.

## Plan amendments made by the controller (all tagged "owner to confirm" in docs/plan.md)
- PD-33..PD-44 + amended PD-6/22/23/26 (see plan "Planning decisions" and spec gap 20).
- Task 28 (T2 follow-up), Task 29 (T7 test hardening), R17 review_closure=0, T27 deps 24/25/26.

## Open decisions (recommendation first)
1. SPEC fundamentals (reviews/spec-1.md #1-#2): branch under review can rewrite .claude/settings.json, CLAUDE.md,
   verify scripts -> add protected_paths (force merge=ask + strong review); verify-fix must get a closure review.
   Recommend: yes to both before T13/T14 code relies on current flow (they are being built now).
2. Default slots_dir under .claude/worktrees collides with Claude Code's own worktrees -> recommend .artel/slots.
3. PD-6 option: blocked/awaiting_accept tasks with a branch hold no area (current) vs hold it (stricter). Recommend current.
4. attempts never reset on stage success (one counter per task) — recommend reset on stage success.
5. R30 accepts any stuck_from (spec: vet/attack before code) — recommend restrict to spec.
6. --review 0: CLI rejects (<1), config allows 0 — recommend allow 0 = no review only with merge=ask.
7. PD-38 adds "error" to default notify.on (spec default differs) — recommend keep.
8. PD-40 assumes api_error_status 429 for a limit hit — verify in T16 live check.
9. fix_resumes_writer=true default rests on unmeasured Q8 — recommend false until T25.
10. autoapprove default for agent-proposed tasks — recommend "none" in v0.1.
