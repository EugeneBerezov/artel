# Spec attack #1 (2026-10-20, independent opus) — FOR THE OWNER (spec changes are the owner's)

FUNDAMENTAL
1. Branch under review controls reviewer and verify (§4 l.173, §3.1 l.136-141, §6 l.341-358): writer (acceptEdits, spike Q5) can commit .claude/settings.json (allow Bash(*)), CLAUDE.md ("approve this"), hooks, package.json "test": "exit 0" -> reviewer not read-only, verify green, auto-merge. Proposal: protected_paths setting (default .claude/, CLAUDE.md, artel.toml, templates, hook dirs, CI config, verify entry points); hit -> merge=ask + strong review; reviewers/verify use integration-tip copies. State that tool profiles stop accidents, not a hostile agent.
2. Verify-fix code merged unreviewed (§3.1 l.100,120): fixing(verify)->verifying has no review; flaky test -> writer loosens/skips test -> auto-merge. Proposal: closure review after verify fix; rerun red verify once before blaming the task; fix touching only tests -> ask.
MAJOR
3. Areas: empty area overlaps everything -> 1 agent at a time unless --area; diff never checked against area (§3 l.66, §5 l.261).
4. Limits rely on unmeasured behavior (§5 l.271-277, spike Q2): pause on utilization >= threshold (0.8 5h / 0.6 weekly); per-task cost cap; is_error+rate-limit = rejected. Weekly window is shared with the owner's own use.
5. accept -> re-verify loop never ends (§3.1 l.118,122,124) — handled in plan as PD (accepted flag).
6. fix_resumes_writer = true default rests on open Q8 (§4 l.204, §9 l.437): default false until T25.
7. Denials are sticky (spike Q4): writer denied pytest once works blind. init derives project_tools from verify; writer permission_denials -> finding for the reviewer.
8. Agent-proposed tasks run unattended by default (§4 l.229-243): default autoapprove="none" in v0.1; cap proposals.
9. No success measure (§1,§2): merged tasks per % weekly quota, human minutes per task; per-task summary for the human merge; `artel revert ID`.
10. Tasks have no acceptance criteria (§3 l.64); reviewers fed writer's notes share its framing: `--accept CRITERIA`; round-1 reviewers don't get writer notes.
12. v0.1 scope: cut vetting/proposals/autoapprove, rotation/resume, stats counterfactual, config set; add protected paths, verify-fix review, utilization pause + budget, acceptance criteria, revert.
MINOR (11): merge left in progress vs slot reset; ready with/without new commits has no reference; no-change task can't finish; uncommitted work wiped on ask (WIP commit); fix failure resets whole branch; one attempts counter; review_rounds arithmetic; own setup_inputs failure pauses the board; notify events omit awaiting_accept/approval/base_conflict/setup_failed; agent question to Telegram unfiltered, stream logs contain .env; parent+slot CLAUDE.md both loaded (unmeasured); base-merge verify names no slot.
