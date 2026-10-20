"""Stages: plan, run, classify and record one agent stage (spec §4, §5 steps 3–6, §3.1).

Decisions come only from the structured result, git state and the board — never from journal text.
"""
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from artel import agent_claude, cmd_agent, journal, procs, prompts, slots, templates, transitions
from artel.agent_claude import RateLimit, StreamParser, validate_result
from artel.board import Task
from artel.errors import ArtelError
from artel.gitutil import INTEGRATION, task_branch
from artel.procs import OnStart
from artel.transitions import Limits, Transition

STATE_TO_STAGE = {"vetting": "vet", "attacking": "attack", "writing": "write",
                  "reviewing": "review", "fixing": "fix"}
SUPPORTED_STAGES = {"attack", "write", "review", "fix"}        # Task 21 adds "vet"
DOWNGRADE_PREFIX = "[downgraded: closure round] "              # PD-4
BLOCKING = ("blocker", "major")


def diff_lines(repo, task_id: int) -> int:
    result = repo.git("diff", "--numstat", f"{INTEGRATION}...{task_branch(task_id)}", check=False)
    if result.returncode != 0:
        return 0
    total = 0
    for line in result.stdout.splitlines():
        added, deleted, _ = line.split("\t", 2)
        total += sum(int(n) for n in (added, deleted) if n.isdigit())   # "-" marks a binary file
    return total


def choose_model(cfg, task: Task, stage: str, diff: int) -> tuple[str, str]:
    if stage == "attack":
        return cfg["models.strong"], "attack stage always uses models.strong"
    if stage in ("write", "fix"):
        return cfg["models.write"], "models.write"
    if stage == "vet":
        return cfg["models.review"], "vet uses models.review"                       # PD-32
    if task.strong:
        return cfg["models.strong"], "task --strong"
    if task.attack:
        return cfg["models.strong"], "task --attack"
    if task.risky:
        return cfg["models.strong"], "task risky (vet)"                             # PD-37
    if diff > cfg["models.strong_diff_lines"]:
        return cfg["models.strong"], f"diff {diff} lines > strong_diff_lines {cfg['models.strong_diff_lines']}"
    return cfg["models.review"], "models.review"


def branchless(task: Task, stage: str) -> bool:
    """Vet and attack run before any code exists: detached at the integration tip (PD-32). A task only reaches
    the attack stage without a branch (the writer creates it later); a vet-triggered attack has `then` set."""
    return stage == "vet" or stage == "attack"


def closure_round(task: Task, stage: str) -> bool:
    return stage == "review" and (task.round >= 2 or task.review_closure)           # PD-4, PD-29


@dataclass(frozen=True)
class StagePlan:
    task_id: int
    stage: str
    round: int
    model: str
    model_reason: str
    profile: str                    # "writer" | "reviewer"
    prompt: str
    argv: list[str]
    env: dict[str, str]
    base_env: dict[str, str]
    log_path: Path
    run_id: int
    head_before: str
    conflict_paths: tuple[str, ...]


@dataclass(frozen=True)
class StageRun:
    result: dict | None
    invalid_reason: str | None
    usage: dict
    exit_code: int | None
    timed_out: bool
    stopped: bool
    rate_limited: bool
    new_commits: bool
    merge_committed: bool
    setup: str                      # "skipped" | "ok" | "failed" | "timeout" | "stopped"
    denials: list[dict]
    session_id: str | None
    first_context: int | None
    last_context: int | None


def _latest_verify_log(repo, task_id: int) -> str:
    directory = repo.logs_dir / str(task_id)
    if not directory.is_dir():
        return ""
    numbered = []
    for path in directory.glob("verify-*.log"):
        stem = path.stem.removeprefix("verify-")
        if stem.isdigit():
            numbered.append((int(stem), path))
    if not numbered:
        return ""
    return max(numbered)[1].read_text(encoding="utf-8", errors="replace")


def plan_stage(repo, board, cfg, task: Task, slot_n: int, *, base_env: Mapping[str, str]) -> StagePlan:
    stage = STATE_TO_STAGE.get(task.state)
    if stage not in SUPPORTED_STAGES:
        raise ArtelError(f"task {task.id} in state {task.state} has no supported stage")
    model, model_reason = choose_model(cfg, task, stage, diff_lines(repo, task.id) if stage == "review" else 0)
    conflict_paths: tuple[str, ...] = ()
    if branchless(task, stage):
        head = slots.prepare_detached(repo, board, cfg, slot_n)
    else:
        head = slots.prepare_slot(repo, board, cfg, slot_n, task.id)
    try:   # from here on any failure must leave the slot (a half-started conflict merge included)
        if stage == "fix" and task.fix_kind == "conflict":
            conflict_paths = tuple(slots.start_conflict_merge(repo, board, cfg, slot_n))
        history = journal.task_entries(board, task.id)
        open_review = journal.open_findings(board, task.id, stages=("review",))
        template = prompts.template_name(stage, task)
        findings = []
        if stage == "write":
            findings = journal.open_findings(board, task.id, stages=("attack",))     # PD-5: given to the writer
        elif stage == "fix":
            findings = open_review
        areas = {t.id: t.area for t in board.list_tasks()}
        context = prompts.select_context(
            task, journal.recent_entries(board, exclude_task=task.id), areas, cfg["context_budget"])
        inputs = prompts.PromptInputs(
            task=task, stage=stage, history=history, findings=findings,
            closure_list=open_review if template == "review-closure.md" else [], context=context,
            verify_log=_latest_verify_log(repo, task.id) if stage == "fix" else "",
            conflict_paths=conflict_paths, diff_range=f"{INTEGRATION}...{task_branch(task.id)}")
        prompt = prompts.render(templates.load_template(repo, cfg, template), inputs,
                                budget=cfg["context_budget"])
        profile = "writer" if stage in ("write", "fix") else "reviewer"
        argv = agent_claude.build_argv(cfg, profile=profile, model=model,
                                       notice_path=templates.notice_path(repo, cfg))
        env = cmd_agent.agent_env(repo, task_id=task.id, stage=stage, round=task.round, base_env=base_env)
        n = 1 + sum(1 for r in board.list_runs(task.id) if r.stage == stage)
        log_path = repo.logs_dir / str(task.id) / f"{stage}-{n}.jsonl"
        run = board.start_run(task_id=task.id, stage=stage, round=task.round, model=model,
                              model_reason=model_reason, slot=slot_n, log_path=str(log_path))
    except BaseException:
        slots.leave_slot(repo, board, cfg, slot_n)
        raise
    return StagePlan(task_id=task.id, stage=stage, round=task.round, model=model, model_reason=model_reason,
                     profile=profile, prompt=prompt, argv=argv, env=env, base_env=dict(base_env),
                     log_path=log_path, run_id=run.id, head_before=head, conflict_paths=conflict_paths)


def _merge_target(repo, path: Path, task: Task, plan: StagePlan) -> str | None:
    """What a conflict fix has to merge: the commit start_conflict_merge left in MERGE_HEAD, or — when the branch
    already contains integration ("Already up to date": a retry after the merge was committed) — its tip."""
    if not (plan.stage == "fix" and task.fix_kind == "conflict"):
        return None
    found = slots._git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=path, check=False)
    return found.stdout.strip() if found.returncode == 0 else repo.rev(INTEGRATION)


def _commit_facts(repo, path: Path, task: Task, plan: StagePlan, merge_target: str | None) -> tuple[bool, bool]:
    """(new_commits, merge_committed), measured on the TASK BRANCH (not on a HEAD the agent may have moved)
    and read before the slot is left (PD-35). The merge is committed when the commit that was to be merged is
    an ancestor of the branch tip: this survives integration moving on while the fixer works, a follow-up
    commit after the merge, and a retry after the merge was already committed."""
    branch = task_branch(task.id)
    tip = repo.rev(branch) or ""
    if plan.stage == "write":
        count = slots._git(repo, "rev-list", "--count", f"{INTEGRATION}..{branch}", cwd=path, check=False)
        new = count.returncode == 0 and int(count.stdout.strip() or 0) > 0
    elif plan.stage == "fix":
        new = bool(tip) and tip != (task.fix_base or plan.head_before)
    else:
        new = False
    merged = False
    if merge_target and tip:
        # BOTH the commit that was to be merged AND the branch tip before the fix are ancestors of the tip: a
        # follow-up commit is fine, `merge --abort; reset --hard artel/integration` (work dropped) is not
        merged = all(slots._git(repo, "merge-base", "--is-ancestor", commit, tip, cwd=path,
                                check=False).returncode == 0 for commit in (merge_target, plan.head_before))
        new = tip != plan.head_before
    return new, merged


def run_stage(repo, board, cfg, plan: StagePlan, slot_n: int, *, on_start: OnStart,
              on_rate_limit: Callable[[RateLimit], None], stop_event: threading.Event) -> StageRun:
    parser = StreamParser()
    setup = "skipped"
    outcome, exit_code, timed_out, stopped = "error", None, False, False
    result, reason = None, None
    new_commits = merge_committed = False
    try:
        task = board.get_task(plan.task_id)
        path = slots.slot_path(repo, cfg, slot_n)
        merge_target = _merge_target(repo, path, task, plan)
        if merge_target is None:                                                    # PD-43: markers break setup
            setup = slots.run_setup_if_needed(repo, board, cfg, slot_n, path, task_id=plan.task_id,
                                              on_start=on_start, stop_event=stop_event, env=plan.base_env)
        if setup in ("failed", "timeout", "stopped"):
            outcome = "setup_" + setup
            stopped = setup == "stopped"
            reason = f"setup {setup}"
        else:
            def on_line(line: str) -> None:
                event = parser.feed(line)
                if event is not None:
                    on_rate_limit(event)

            res = procs.run_tree(
                plan.argv, cwd=path, env=plan.env, stdin_text=plan.prompt, timeout=cfg["stage_timeout"],
                kill_grace=cfg["kill_grace"], log_path=plan.log_path, on_line=on_line, stop_event=stop_event,
                on_start=lambda pid, group, fp: on_start("agent", pid, group, fp))
            exit_code, timed_out, stopped = res.returncode, res.timed_out, res.stopped
            if parser.limit_hit() and parser.max_rank < 2:       # PD-40: only the error result showed the limit
                on_rate_limit(RateLimit(status=f"error {parser.api_error_status}", rank=2, resets_at=None,
                                        limit_type=None, utilization=None, windows={}))
            if not parser.failed():
                result, reason = validate_result(plan.stage, parser.structured)
            elif not parser.result_seen:
                reason = "the agent produced no result"
            elif parser.is_error:
                reason = "the agent reported an error"
            else:
                reason = "the agent returned no structured output"
            new_commits, merge_committed = _commit_facts(repo, path, task, plan, merge_target)
            outcome = ("stopped" if stopped else "timeout" if timed_out else
                       result["outcome"] if result else "rate_limited" if parser.limit_hit() else "invalid")
    finally:
        try:
            slots.leave_slot(repo, board, cfg, slot_n)
        finally:
            usage = parser.usage
            board.finish_run(plan.run_id, outcome=outcome, usage=usage.to_dict(), exit_code=exit_code,
                             timed_out=timed_out, killed=timed_out or stopped,
                             first_context=usage.first_context_tokens(), last_context=usage.context_tokens(),
                             turns=usage.num_turns, duration_ms=usage.duration_ms, cost_usd=usage.cost_usd,
                             session_id=parser.session_id)
    return StageRun(result=result, invalid_reason=reason, usage=usage.to_dict(), exit_code=exit_code,
                    timed_out=timed_out, stopped=stopped, rate_limited=parser.limit_hit(),
                    new_commits=new_commits, merge_committed=merge_committed, setup=setup,
                    denials=list(parser.denials), session_id=parser.session_id,
                    first_context=usage.first_context_tokens(), last_context=usage.context_tokens())


def _effective_severity(task: Task, stage: str, severity: str) -> str:
    return severity if severity == "blocker" or not closure_round(task, stage) else "minor"   # PD-4


def classify(task: Task, stage: str, run: StageRun, board) -> str:
    if run.stopped:
        return "requeue"
    if run.rate_limited and run.result is None:                                     # PD-40
        return "requeue"
    if run.setup in ("failed", "timeout"):
        return "setup_failed"
    if run.timed_out or run.result is None:
        return "stage_failed"
    result = run.result
    outcome = result["outcome"]
    if outcome == "fail":
        return "stage_failed"
    if outcome == "ask":
        return "ask"
    if stage == "attack":
        blockers = any(f["severity"] == "blocker" for f in result["findings"])
        return "attack_blocker" if blockers else "attack_clear"
    if stage in ("write", "fix"):
        if task.fix_kind == "conflict" and stage == "fix":
            return "ready" if run.merge_committed else "stage_failed"
        return "ready" if run.new_commits else "stage_failed"                       # PD-35
    if stage == "review":
        open_ids = {e.id: e for e in journal.open_findings(board, task.id)}
        closed = {i for i in result["closed"] if i in open_ids}                     # PD-42: other ids are ignored
        still_open = [e for i, e in open_ids.items() if i not in closed]
        new = [f for f in result["findings"] if _effective_severity(task, stage, f["severity"]) in BLOCKING]
        blocking = any(e.severity in BLOCKING for e in still_open) or bool(new)     # PD-3
        return "review_blocking" if blocking else "review_clear"
    raise ValueError(f"unsupported stage: {stage!r}")


def _clip(text: str, limit: int = journal.TEXT_LIMIT) -> str:
    return journal.truncate_utf8(" ".join(text.split()), limit)


def _clip_tail(prefix: str, text: str, limit: int = journal.TEXT_LIMIT) -> str:
    """prefix + text, whitespace folded; when too long the START of `text` is cut, because the end of a
    traceback names the error."""
    text = " ".join(text.split())
    room = limit - len(prefix.encode("utf-8"))
    raw = text.encode("utf-8")
    if len(raw) <= room:
        return prefix + text
    marker = "…".encode("utf-8")
    return prefix + (marker + raw[-(room - len(marker)):]).decode("utf-8", errors="ignore")


def record_stage(board, task: Task, plan: StagePlan, run: StageRun, event: str) -> None:
    author = f"agent:{plan.stage}"
    common = dict(task_id=task.id, stage=plan.stage, round=plan.round)
    result = run.result
    judged = result is not None and result["outcome"] in ("pass", "findings")   # not for fail / ask results
    if judged and plan.stage == "review":
        open_ids = {e.id for e in journal.open_findings(board, task.id)}
        for finding_id in dict.fromkeys(result["closed"]):
            if finding_id in open_ids:
                journal.close_finding(board, finding_id, task_id=task.id, round=plan.round,
                                      basis=f"review r{plan.round}", author=author)
            else:                                                                    # PD-42
                journal.append(board, "FAIL", _clip(f"closed #{finding_id} ignored: not an open finding of #{task.id}"),
                               author="runner", **common)
    if judged and plan.stage in ("attack", "review"):
        for finding in result["findings"]:
            severity = _effective_severity(task, plan.stage, finding["severity"])
            text = finding["text"]
            if severity != finding["severity"]:
                text = DOWNGRADE_PREFIX + text                                       # PD-4
            journal.add_finding(board, task_id=task.id, stage=plan.stage, round=plan.round, severity=severity,
                                text=journal.truncate_utf8(text, journal.TEXT_LIMIT),            # PD-21
                                basis=journal.truncate_utf8(finding["basis"], journal.BASIS_LIMIT), author=author)
    if event == "ask":
        journal.append(board, "ASK", _clip(result["question"]), author=author, **common)
    if event == "setup_failed":
        journal.append(board, "FAIL", f"setup {run.setup}: the stage was not started (see the setup log)",
                       author="runner", **common)
    for denial in run.denials:                                                       # PD-24
        detail = json.dumps(denial.get("tool_input"), ensure_ascii=False)
        journal.append(board, "FAIL", _clip(f"denied: {denial.get('tool_name')} {detail}"),
                       author="runner", **common)
    label = {"ready": "READY", "review_clear": "PASS", "attack_clear": "PASS", "review_blocking": "FINDINGS",
             "attack_blocker": "FINDINGS", "stage_failed": "FAIL"}.get(event)
    if label is not None:
        head = f"{label} {plan.stage} r{plan.round} model={plan.model} ({plan.model_reason})"
        if event == "stage_failed":
            if run.timed_out:
                why = "timed out"
            elif result is None:
                why = run.invalid_reason or "no valid result"
            elif result["outcome"] == "fail":
                why = f"outcome fail: {result['summary']}"
            elif plan.stage == "fix" and task.fix_kind == "conflict":
                why = "ready without a merge commit"
            else:
                why = "ready without new commits"
            head += f": {why}"
        elif result is not None and result["summary"]:
            head += f": {result['summary']}"
        journal.append(board, "VERDICT", _clip(head), author="runner", **common)


def _ask_open_findings(board, task_id: int, common: dict) -> None:
    open_now = journal.open_findings(board, task_id)
    listing = "; ".join(f"#{e.id} [{e.severity}] {e.text}" for e in open_now) or "(none)"
    journal.append(board, "ASK", _clip("open findings after the last review round: " + listing),
                   author="runner", **common)


def _cause_verdict(board, tr: Transition, plan: StagePlan, common: dict) -> None:
    if not tr.cause:
        return
    if tr.rid == "R22a":   # review_blocking below the cap -> fixing: name review_rounds (§10)
        journal.append(board, "VERDICT", _clip(f"FIX {plan.stage} r{plan.round}: {tr.cause}"), author="runner", **common)
        return
    if tr.to_state in ("blocked", "stuck") or tr.event in ("stage_failed", "internal_error"):
        word = tr.to_state.upper() if tr.to_state in ("blocked", "stuck") else "RETRY"
        journal.append(board, "VERDICT", _clip(f"{word} {plan.stage} r{plan.round}: {tr.cause}"),
                       author="runner", **common)                                    # names the setting


def finish_stage(repo, board, cfg, task_id: int, plan: StagePlan, run: StageRun) -> Transition | None:
    task = board.get_task(task_id)
    if STATE_TO_STAGE.get(task.state) != plan.stage:   # e.g. rejected while the job ran (PD-30): nothing to apply
        board.clear_running(task_id)
        return None
    event = classify(task, plan.stage, run, board)
    if event == "requeue":
        board.clear_running(task_id)
        return None
    try:
        with board.tx():   # all or nothing: a failed apply must not leave findings that a retry would duplicate
            record_stage(board, task, plan, run, event)
            tr = transitions.apply(board, task_id, event, Limits.from_cfg(task, cfg), stage=plan.stage,
                                   has_branch=repo.branch_exists(task_branch(task_id)),
                                   tip=repo.rev(task_branch(task_id)))
            if "reset_branch" in tr.effects:   # PD-46: the code those findings were about is gone
                for finding in journal.open_findings(board, task_id, stages=("review",)):
                    journal.close_finding(board, finding.id, task_id=task_id, round=plan.round,
                                          basis="superseded: branch reset (R17)", author="artel")
    finally:
        board.clear_running(task_id)
    common = dict(task_id=task_id, stage=plan.stage, round=plan.round)
    if "ask_open_findings" in tr.effects:
        _ask_open_findings(board, task_id, common)
    _cause_verdict(board, tr, plan, common)
    slots.apply_git_effects(repo, board, cfg, task_id, tr.effects)
    return tr


def finish_internal_error(repo, board, cfg, task_id: int, error_tail: str) -> Transition:
    task = board.get_task(task_id)
    stage = STATE_TO_STAGE.get(task.state, task.state)   # verifying has no agent stage; its state names the job
    try:
        journal.append(board, "FAIL", _clip_tail("internal error: ", error_tail), author="runner",
                       task_id=task_id, stage=stage)
        tr = transitions.apply(board, task_id, "internal_error", Limits.from_cfg(task, cfg),
                               has_branch=repo.branch_exists(task_branch(task_id)),
                               tip=repo.rev(task_branch(task_id)))
    finally:
        board.clear_running(task_id)
    _cause_verdict(board, tr, StagePlan(task_id, stage, task.round, "", "", "", "", [], {}, {}, Path(), 0, "", ()),
                   dict(task_id=task_id, stage=stage, round=task.round))
    return tr
