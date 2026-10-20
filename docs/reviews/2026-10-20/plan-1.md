# Plan attack #1 (2026-10-20, two independent opus reviewers: plan-internal "I", plan-vs-spec "S")

## Blockers
B1 (I,S) T4 schema: column `then` is an SQLite keyword (L743,657,767) -> syntax error. Quote "then" everywhere; update_task quotes all column names; add a test.
B2 (I) T10 pinned signatures: delete_task_branch(repo, task_id) / apply_git_effects(repo, board, task_id, effects) must call leave_slot/guard(repo, board, cfg, n) -> need board and cfg (L1626-1631); callers T13 (L1874), T18 (L2179).
B3 (I) merge_identity_args pinned in T14 (W5, L1921) but needed by T10 start_conflict_merge (W4, L1607); integrate imports slots -> cycle. Define in slots (T10) or gitutil; T14 imports it.
B4 (I) PD-8 vs T15: SlotBusy raised in worker plan_stage/prepare_slot (L1602,1844) after main applied pick (L2043) -> Job.error -> finish_internal_error counts an attempt (L2036); PD-8 says skip. Fix: tick checks slots.checked_out_elsewhere before dispatch, or worker maps SlotBusy to requeue.

## Major
M1 (I,S) Same task dispatched twice: pick.select excludes only running=True (L1077); running set in on_start after prepare+setup (L2051,1383); verify even later. Fix: select excludes tasks with a live in-memory Job (T15), or set_running at dispatch.
M2 (I) Runner(env=...) never reaches agents: plan_stage has no env (L1844), agent_env needs base_env (L1721). Add base_env kwarg.
M3 (I) Fake agent call record has no pid (L1520) but E2ECase tearDown asserts every pid in calls.jsonl is dead (L2052). T9 records "pid": os.getpid().
M4 (I) Proposed tasks never moved to vetting: T21 assumes runner picks proposed->vetting (L2307) but T15 applies pick only for open (L2043); T22 owns runner.py in W8. T15 applies R03 to proposed when "vet" in SUPPORTED_STAGES.
M5 (I) Meta value formats not pinned: T19 (W7) reads limit:<type>, pause_until, heavy_lock_stale (L2236) written by T22 (W8, L2343). Pin at L767 (JSON for limit:*, unix seconds string for pause_until).
M6 (I) T13/T14 same wave; "import limits_for if T13 merged" (L1917) never true -> duplicated logic. Move to T7 as Limits.from_cfg(task, cfg).
M7 (I) T8 test_heavy_argv_forms expects literal ["sh","-c",...] but shell_argv resolves which("sh"); heavy_argv has no windows param (L1436). Compare against platform.prefix_argv(...) or patch platform.which.
M8 (I) Base job has no task: runs.task_id NOT NULL (L756), setup/verify logs under logs/<id>/ (L1621,1932), set_running needs a row though running_kind has "base" (L663), finish_internal_error(task_id) for any failed job (L2036). Pin a sentinel or base pids in meta + logs/base/.
M9 (I,S) T9 Windows: test_inline_schema_survives_cmd_wrapper calls subprocess.run without env= (L1561) -> fake_agent can't import artel on CI; .cmd quoting of inline --json-schema breaks under cmd.exe (gap 12, "owner decision" stalls T9). Decide now: resolve claude.cmd to node + script, prefer claude.exe; pass env=subprocess_env().
M10 (I) Board can't add/skip tasks; T27 depends only on 26 which may be skipped. T27 **Depends on:** Task 24. (board.py now has reopen and init --refresh; skip = `done ID --note skipped`.)
S2 (S) terminate_tree leader-gone returns False (T3 L522, merged) -> orphans; T8 run_tree "returns after reader threads drained" (L1387) hangs. POSIX killpg group whenever tree_alive(group); T8 kills group/job after leader exit before joining readers, or drains with a deadline.
S3 (S) "ready without new commits" baseline (T13 L1858, R17): new_commits = HEAD != head_before at run start; after requeue or ask->answer a writer whose commits already landed returns ready with no new commit -> stage_failed -> branch reset, attempt counted (forbidden by §3.1 requeue rule). New PD: store commit at entry into the state (writing: integration tip; fix: reviewed head) and compare to that.
S4 (S) Conflict fix runs setup on conflict markers (T13 L1845/1855): start_conflict_merge before run_setup_if_needed -> conflicted lockfile -> setup_failed pause forever. Run setup before the merge, or skip setup for fix_kind=conflict.
S5 (S) Area exclusion (PD-6, T6 select 1-2): blocked/stuck/awaiting_accept tasks hold no area; answered/due tasks selected with no overlap check -> overlapping tasks run together. Tasks with a branch hold their area in every live state, or steps 1-2 check overlap vs running.
S6 (S) Usage-limit detection (PD-22, T9, T13): spike saw only status "allowed" once before result. A limit hit likely ends with is_error/api_error_status -> stage_failed, attempt+1, writer reset (contradicts §5 requeue with no counter). And rank 2 requeues even with a valid structured result. Treat is_error + limit api_error_status as rank 2; with a valid result apply it and only pause.
S7 (S) Hallucinated closure id costs an attempt (T13 record_stage, T6 close_finding raises). Validate `closed` vs open findings in validate_result/classify; drop unknown ids with a FAIL note.
S5b/S11 Spec contradiction accept loop: accept -> verified -> ff fails -> verifying -> green -> merge=ask -> awaiting_accept again (R31b); plan test test_accept_then_integration_moved_reverifies_then_done expects done. PD: `accepted` flag -> re-verify after accept goes to verified.

## Minor
- cli.py defaults --body/--basis/--reason None vs NOT NULL body/basis and pinned "" defaults: T11/T18 must coerce.
- Plan excerpts break own rules: TemporaryDirectory() without ignore_cleanup_errors (L794,1101); write_text without encoding (L992).
- $stage substituted in render but PromptInputs has no stage field (L1767-1773).
- T11 needs tests/test_zipapp.py and pyproject packages (artel.builtin_templates) not in its Files (L1665,1735).
- argv setting kind uses shlex.split(posix=True): C:\Tools\claude.exe -> C:Toolsclaude.exe (L872).
- on_start(pid, group, fp) has no kind (L1377,1618).
- transitions.create has no Limits param but decide requires one (L1200).
- SEVERITIES equality test has no owner (T6 || T9) (L1455).
- T17 Files list names fake_telegram.py twice (L2111).
- choose_model escalates reviews only on task.attack; vet-triggered risky -> silent reading; add PD.
- Base moved: picks in the same tick as base job start; §5 "before the next pick": no picks while base update pending/running.
- R34b (conflict at cap -> blocked) has no spec row; name as PD.
- setup_failed/base_conflict pauses must notify (§3.1): no T15/T22 test; internal errors have no event kind.
- T17: notify test uses enable=False but §7 says notify test enables after success.
- T18: artel stop with no runner leaves a stop file -> next run exits at once; don't enqueue, clear stale at start.
- T5: [rotation] settings shown active even if T26 skipped.
- review_rounds=2 allows 1 review fix, README §11.6 says "up to 2 fixes" — name the arithmetic.
- PD-23: summing assistant usages may double-count per content block; dedupe by message.id.
- T6 IN_FLIGHT says attack with then=None holds its area; PD-6 says attack-before-code never does.
- Risky splits suggested: T13 (13a plan/model/prompt, 13b run/classify/record/finish), T22 (a reconcile/stop/signals, b pauses/limits/disk/logs/notifier), T9 (adapter vs fake agent).
