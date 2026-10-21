"""Verify on the merged tree, guarded fast-forward of artel/integration, and the base-moved job (spec §5).

`artel/integration` is only ever moved here, by `git update-ref` with the old value as a guard; nothing in
this module pushes or checks that branch out.
"""
import dataclasses
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from artel import journal, pauses, procs, protect, slots, testguard, transitions
from artel.board import Task
from artel.errors import ArtelError, GitError
from artel.gitutil import INTEGRATION, Repo, task_branch

OnStart = procs.OnStart
_INTEGRATION_REF = f"refs/heads/{INTEGRATION}"


def _verified_key(task_id: int) -> str:
    return f"verified:{task_id}"
_RUNNER = "runner"


def ensure_integration_branch(repo: Repo, base: str) -> str:
    sha = repo.rev(INTEGRATION)
    if sha is not None:
        return sha
    tip = repo.rev(base)
    if tip is None:
        raise ArtelError(f"base branch {base!r} does not exist; set `base` in artel.toml or create the branch")
    repo.git("branch", INTEGRATION, tip)
    return tip


# --- verify on the merged tree ------------------------------------------------------------------------------

@dataclass(frozen=True)
class VerifyRun:
    outcome: str         # "green" | "red" | "timeout" | "stopped" ("conflict" for Task 40, never a runs row)
    duration_ms: int
    log_path: Path
    ids: frozenset[str] | None = None   # passed test ids read from the log (green runs only, PD-64)


@dataclass(frozen=True)
class VerifyResult:
    kind: str            # "green" | "green_flaky" | "red" | "conflict" | "timeout" | "stopped" | "setup_failed"
    commit: str | None   # task branch tip after the merge (green and green_flaky only)
    log_path: Path
    detail: str
    runs: tuple[VerifyRun, ...] = ()
    ids: frozenset[str] | None = None   # the deciding green run's passed test ids
    base_sha: str | None = None         # the integration tip this verify merged (the id baseline, PD-64)


def _rel(repo: Repo, path: Path) -> str:
    try:
        return path.relative_to(repo.common_dir).as_posix()
    except ValueError:
        return str(path)


def _conflicts(repo: Repo, path: Path) -> list[str]:
    out = slots._git(repo, "diff", "--name-only", "--diff-filter=U", "-z", cwd=path).stdout
    return [name for name in out.split("\0") if name]


def _merge_into_slot(repo: Repo, path: Path, ref: str) -> list[str] | None:
    """Merge `ref` in the slot. None = merged cleanly; else the conflicting paths (the merge stays open for
    leave_slot to abort)."""
    merged = slots._git(repo, *slots.merge_identity_args(repo), "merge", "--no-edit", ref, cwd=path, check=False)
    if merged.returncode == 0:
        return None
    files = _conflicts(repo, path)
    if not files:
        raise GitError(["merge", "--no-edit", ref], merged.stderr or merged.stdout)
    return files


def _verify_command(cfg, repo: Repo, path: Path, *, log_path: Path, env, on_start, stop_event) -> tuple[str, str, int | None]:
    """Run commands.verify under the heavy lock. -> (kind, detail, exit code); kind green|red|timeout|stopped."""
    verify = cfg["commands.verify"]
    try:
        argv = procs.heavy_argv(verify, cfg["commands.heavy_lock"], cfg["shell"])
        result = procs.run_tree(
            argv, cwd=path, env=env, timeout=cfg["verify_timeout"], kill_grace=cfg["kill_grace"], log_path=log_path,
            merge_stderr=True, stop_event=stop_event,
            on_start=None if on_start is None else (lambda pid, group, fp: on_start("verify", pid, group, fp)))
    except ArtelError as exc:   # the command could not start: red, with the reason in the log
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"artel: verify could not start: {exc}\n")
        return "not_started", f"verify could not start: {exc}", None   # an environment error, not a red tree
    if result.stopped:
        return "stopped", "stopped", result.returncode
    if result.timed_out:
        return "timeout", f"timed out after {cfg['verify_timeout']:g}s (verify_timeout)", result.returncode
    if result.returncode == 0:
        return "green", "exit 0", 0
    return "red", f"exit {result.returncode}", result.returncode


def _mark_red_seen(board, task_id: int) -> None:
    """red_seen=1 as soon as a run came back `red` or `timeout` (PD-72), whatever the job's end. `not_started` and
    an internal error are no red tree. A task that moved on (rejected, terminated) needs no flag. The write keeps
    `version`: the runner tells "result already applied" by it."""
    board.set_red_seen(task_id, expect_state="verifying")


def _verify_job(repo: Repo, board, cfg, task: Task, slot_n: int, path: Path, commit: str, *, env, on_start,
                stop_event, queued_at) -> VerifyResult:
    """Run commands.verify, and again after a red run (spec §5 step 7): the same merged tree, no setup, each run
    under its own heavy_lock, one `runs` row each. Green on a rerun is flaky. A task that has been red before
    (`red_seen`, PD-72) needs every one of its 1 + verify_reruns runs green."""
    total = 1 + cfg["verify_reruns"]
    strict = task.red_seen
    runs: list[VerifyRun] = []
    detail = ""
    for i in range(total):
        if i and stop_event is not None and stop_event.is_set():   # no process, no lock for a stopped rerun
            return VerifyResult("stopped", None, runs[-1].log_path, "stopped before a rerun", tuple(runs))
        n = 1 + sum(1 for r in board.list_runs(task.id) if r.stage == "verify")
        log_path = repo.logs_dir / str(task.id) / f"verify-{n}.log"
        run = board.start_run(task_id=task.id, stage="verify", round=task.round, model=None, model_reason=None,
                              slot=slot_n, log_path=str(log_path), queued_at=queued_at)
        kind, detail, code = "error", "internal error", None
        started = time.monotonic()
        try:
            kind, detail, code = _verify_command(cfg, repo, path, log_path=log_path, env=env, on_start=on_start,
                                                 stop_event=stop_event)
        finally:
            outcome = "red" if kind in ("not_started", "error") else kind   # the runs vocabulary has no other
            duration_ms = int((time.monotonic() - started) * 1000)
            board.finish_run(run.id, outcome=outcome, usage={}, exit_code=code, timed_out=kind == "timeout",
                             killed=kind in ("timeout", "stopped"), duration_ms=duration_ms)
            if kind in ("red", "timeout"):   # persist the evidence now: a stop, an error or a crash in the rerun must not forget it
                _mark_red_seen(board, task.id)
        ids = testguard.ids_from_log(log_path, cfg["verify_test_id_regex"]) if outcome == "green" else None
        runs.append(VerifyRun(outcome, duration_ms, log_path, ids))
        if kind == "not_started":   # like a failing setup: pause the board, burn no fix and no agent
            return VerifyResult("setup_failed", None, log_path, detail, tuple(runs))
        if kind in ("timeout", "stopped"):   # never rerun
            return VerifyResult(kind, None, log_path, detail, tuple(runs))
        if kind == "green" and not strict:
            break
    outcomes = [r.outcome for r in runs]
    last = runs[-1].log_path
    deciding = next((r.ids for r in reversed(runs) if r.outcome == "green"), None)
    if all(o == "green" for o in outcomes):
        return VerifyResult("green", commit, last, detail, tuple(runs), deciding)
    if all(o == "red" for o in outcomes):
        return VerifyResult("red", None, last, detail, tuple(runs))
    first_red = next(r for r in runs if r.outcome == "red")
    return VerifyResult("green_flaky", commit, first_red.log_path,
                        f"mixed results ({', '.join(outcomes)}; verify_reruns {cfg['verify_reruns']})",
                        tuple(runs), deciding)


def verify_task(repo: Repo, board, cfg, task: Task, slot_n: int, *, on_start: OnStart | None = None,
                stop_event=None, env: Mapping[str, str] | None = None, queued_at: float | None = None
                ) -> VerifyResult:
    n = 1 + sum(1 for r in board.list_runs(task.id) if r.stage == "verify")
    log_path = repo.logs_dir / str(task.id) / f"verify-{n}.log"
    if not repo.branch_exists(task_branch(task.id)):   # prepare_slot would re-create it empty: green on nothing
        raise ArtelError(f"cannot verify task {task.id}: {task_branch(task.id)} does not exist (internal error)")
    slots.prepare_slot(repo, board, cfg, slot_n, task.id)
    path = slots.slot_path(repo, cfg, slot_n)
    try:
        base_sha = repo.rev(INTEGRATION)   # merged by sha: the id baseline is exactly what the tree contains
        if base_sha is None:
            raise ArtelError(f"cannot verify task {task.id}: {INTEGRATION} does not exist (internal error)")
        files = _merge_into_slot(repo, path, base_sha)
        if files is not None:
            detail = "conflict in " + ", ".join(files)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"artel: merging {INTEGRATION} into {task_branch(task.id)}: {detail}\n")
            return VerifyResult("conflict", None, log_path, detail)
        setup = slots.run_setup_if_needed(repo, board, cfg, slot_n, path, task_id=task.id, on_start=on_start,
                                          stop_event=stop_event, env=env)
        if setup == "stopped":
            return VerifyResult("stopped", None, log_path, "stopped during setup")
        if setup in ("failed", "timeout"):
            setup_logs = sum(1 for r in board.list_runs(task.id) if r.stage == "setup")
            return VerifyResult("setup_failed", None, repo.logs_dir / str(task.id) / f"setup-{setup_logs}.log",
                                f"setup {setup}")
        commit = slots._git(repo, "rev-parse", "HEAD", cwd=path).stdout.strip()
        if not cfg["commands.verify"]:
            return VerifyResult("green", commit, log_path, 'verify not configured (commands.verify="")',   # PD-14
                                base_sha=base_sha)
        result = _verify_job(repo, board, cfg, task, slot_n, path, commit, env=env, on_start=on_start,
                             stop_event=stop_event, queued_at=queued_at)
        return dataclasses.replace(result, base_sha=base_sha)
    finally:
        slots.leave_slot(repo, board, cfg, slot_n)


_EVENT = {"green": "verify_green", "green_flaky": "verify_green_flaky", "red": "verify_red",
          "timeout": "verify_red", "conflict": "verify_conflict", "setup_failed": "setup_failed"}


def gates(repo: Repo, board, cfg, task: Task, result: VerifyResult) -> list[protect.Hit]:
    """What stops a green verify from merging on its own: protected paths, the test guard's hard hits, a passed
    test that went missing, required tests, an empty verify (PD-47, PD-64, PD-14). The verified commit is
    inspected, not a later one (PD-45)."""
    commit = result.commit or task_branch(task.id)
    found = protect.hits(repo, cfg, commit)
    if not any(h.path == protect.GIT_ERROR for h in found):   # the same git failure would only repeat
        found += testguard.hard_hits(repo, cfg, commit)
    hit = testguard.id_hit(repo, board, cfg, result)
    if hit is not None:
        found.append(hit)
    found += testguard.required_hits(testguard.required_tests(task.body), result.ids)
    if not cfg["commands.verify"]:
        found.append(protect.Hit(testguard.VERIFY, "commands.verify", 'verify not configured (commands.verify="")'))
    return found


_PROTECT_SOURCES = ("protected_paths", "templates_dir", "commands.verify", "commands.setup", "commands.setup_inputs")


def _gate_reason(guarded: list[protect.Hit]) -> str:
    """`<reason>; <reason>; <reason>; +N more (<settings>)`, protected paths first (PD-47), then one reason per
    other hit; the first line git printed for a failed protected-path check follows the settings."""
    protected = [h for h in guarded if h.source in _PROTECT_SOURCES and h.path != testguard.VERIFY]
    reasons = [f"touches protected path(s) {protect.summary(protected)}"] if protected else []
    for h in guarded:
        if h not in protected:
            reasons.append(h.note if h.path == testguard.VERIFY else f"{h.path} {h.note}")
    text = "; ".join(reasons[:protect.SHOWN])
    if len(reasons) > protect.SHOWN:
        text += f"; +{len(reasons) - protect.SHOWN} more"
    text += f" ({', '.join(sorted({h.source for h in guarded}))})"
    why = "; ".join(sorted({h.note for h in protected if h.path == protect.GIT_ERROR}))
    return text + (f"; {why}" if why else "")


def finish_verify(repo: Repo, board, cfg, task_id: int, result: VerifyResult):
    if result.kind == "stopped":
        board.clear_running(task_id)
        return None
    task = board.get_task(task_id)
    limits = transitions.Limits.from_cfg(task, cfg)
    tip = repo.rev(task_branch(task_id))   # after the verify merge: the base of a verify fix (PD-35)
    guarded: list[protect.Hit] = []
    if (result.kind == "green" and limits.merge == "auto") or result.kind == "green_flaky":
        guarded = gates(repo, board, cfg, task, result)
        if guarded and result.kind == "green":
            limits = dataclasses.replace(limits, merge="ask")   # R31b unless accepted (R31a, PD-36)
    flaky_text = ""
    if result.kind == "green_flaky":   # a FAIL, never a FACT: the first run was red
        failed = failed_tests(result.log_path)
        flaky_text = f"flaky verify: {_command_text(cfg['commands.verify'])}, red log {_rel(repo, result.log_path)}"
        if failed:
            flaky_text += "; failed: " + ", ".join(failed)
    transitions.decide(task, _EVENT[result.kind], limits, tip=tip)   # refuse before writing anything
    if result.kind in ("green", "green_flaky") and result.commit and result.ids is not None:
        try:   # the id baseline of this sha, for a later integrate_task; a failed write only turns the check off
            testguard.write_ids(repo, result.commit, result.ids)
        except OSError:
            pass
    with board.tx():   # the transition, the verified commit and the journal lines land together
        if result.kind != "setup_failed":
            if not board.update_task(task_id, expect_state=task.state, expect_version=task.version,
                                     last_verify=result.kind):
                raise transitions.StaleTask(f"task {task_id} changed under us (verify result)")
        tr = transitions.apply(board, task_id, _EVENT[result.kind], limits, tip=tip)
        if guarded and tr.rid in ("R31b", "R31c"):
            lead = "merge=ask: flaky verify (verify_reruns); " if tr.rid == "R31c" else "merge=ask: "
            tr = dataclasses.replace(tr, cause=lead + _gate_reason(guarded))
        elif tr.rid == "R31c":
            tr = dataclasses.replace(tr, cause="merge=ask: flaky verify (verify_reruns)")
        if result.kind in ("green", "green_flaky") and result.commit:
            board.set_meta(_verified_key(task_id), result.commit)   # PD-45: exactly this commit may be integrated
        elif result.kind != "setup_failed":
            board.del_meta(_verified_key(task_id))
        log = _rel(repo, result.log_path)
        verdict = f"verify {result.kind}: {result.detail}; {tr.from_state} -> {tr.to_state} [{tr.rid}]"
        if tr.cause:
            verdict += f" ({tr.cause})"
        journal.append(board, "VERDICT", journal.truncate_utf8(verdict, journal.TEXT_LIMIT), author=_RUNNER,
                       task_id=task_id, stage="verify", round=task.round, basis=journal.truncate_utf8(log, 200))
        off = testguard.id_check_off(repo, cfg, result) if result.kind in ("green", "green_flaky") else None
        if off:   # phase 1: the controller must see that the id rule did not run
            journal.append(board, "VERDICT", journal.truncate_utf8(off, journal.TEXT_LIMIT), author=_RUNNER,
                           task_id=task_id, stage="verify", round=task.round, basis=journal.truncate_utf8(log, 200))
        ignored = testguard.required_ignored(task.body) if result.kind in ("green", "green_flaky") else []
        if ignored:   # a `Tests:` item that names no test is no requirement (spec §4); say so once per verify
            note = "Tests: item(s) naming no test ignored: " + ", ".join(i[:40] for i in ignored[:3])
            journal.append(board, "FACT", journal.truncate_utf8(note, journal.TEXT_LIMIT), author=_RUNNER,
                           task_id=task_id, stage="verify", round=task.round, basis=journal.truncate_utf8(log, 200))
        if result.kind in ("red", "timeout"):
            journal.append(board, "FAIL", journal.truncate_utf8(
                f"verify {result.kind} ({result.detail}); log: {log}", journal.TEXT_LIMIT),
                           author=_RUNNER, task_id=task_id, stage="verify", round=task.round,
                           basis=journal.truncate_utf8(log, 200))
        elif flaky_text:
            journal.append(board, "FAIL", journal.truncate_utf8(flaky_text, journal.TEXT_LIMIT), author=_RUNNER,
                           task_id=task_id, stage="verify", round=task.round, basis=journal.truncate_utf8(log, 200))
    return tr


def _command_text(command, limit: int = 100) -> str:
    """(`limit` in bytes) The command for a journal line, cut so the log path and failed tests after it survive the 400-byte limit."""
    text = " ".join(command) if isinstance(command, (list, tuple)) else str(command)
    text = " ".join(text.split())
    return journal.truncate_utf8(text, limit)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


_PERCENT = re.compile(r"\s*\[\s*\d+%\]\s*$")


def _failed_id(line: str) -> str | None:
    line = _ANSI.sub("", line).rstrip("\r\n")
    if line.startswith("FAILED ("):   # unittest's summary line
        return None
    if line.startswith(("FAIL: ", "ERROR: ")):
        return line.split(": ", 1)[1].strip() or None
    if line.startswith("FAILED "):
        return line[len("FAILED "):].split(" - ", 1)[0].strip() or None
    if " FAILED " in line:
        before, after = line.split(" FAILED ", 1)
        node = after.split(" - ", 1)[0].strip()
        if "::" in node:   # pytest-xdist: `[gw0] [ 50%] FAILED tests/t.py::test_a`
            return node
        return _PERCENT.sub("", before).strip() or None   # `tests/t.py::test_a FAILED [ 50%]`
    return None


def failed_tests(log_path: Path, limit: int = 3) -> list[str]:
    """Up to `limit` failed test ids from a verify log: unittest `FAIL: id` / `ERROR: id`, pytest `FAILED id - why`,
    `id FAILED [ 50%]` or xdist `[gw0] [ 50%] FAILED id`. Reads the log line by line."""
    found: list[str] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace", newline="") as fh:
            for line in fh:
                test = _failed_id(line)
                if test and test not in found:
                    found.append(test)
                    if len(found) == limit:
                        break
    except OSError:
        return []
    return found


# --- integrate ----------------------------------------------------------------------------------------------

def _count(n: int) -> str:
    return f"{n / 1000:.1f}k"


def _usage_summary(board, task_id: int) -> str:
    per_stage: dict[str, list[int]] = {}
    for run in board.list_runs(task_id):
        usage = run.usage or {}
        tokens_in = sum(int(usage.get(k) or 0) for k in
                        ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        tokens_out = int(usage.get("output_tokens") or 0)
        if tokens_in or tokens_out:
            total = per_stage.setdefault(run.stage, [0, 0])
            total[0] += tokens_in
            total[1] += tokens_out
    return ", ".join(f"{stage} {_count(a)}/{_count(b)}" for stage, (a, b) in per_stage.items())


def _last_verify_log(repo: Repo, board, task_id: int) -> str:
    logs = [r.log_path for r in board.list_runs(task_id) if r.stage == "verify" and r.log_path]
    return _rel(repo, Path(logs[-1])) if logs else ""


def _record_base(repo: Repo, board, sha: str, *, ids: frozenset[str] | None, log: str,
                 at: float | None = None) -> None:
    """The integration tip `sha` has a green verify: the base-check cache and the id baseline (PD-64). The ref
    has moved already; a failed write only turns the id check off."""
    try:
        testguard.write_base(board, repo, sha=sha, green=True, at=time.time() if at is None else at, log=log,
                             ids=ids)
    except OSError:
        pass


def integrate_task(repo: Repo, board, cfg, task_id: int) -> str:
    """-> "done" | "held" | "retry" | "reverify"."""
    task = board.get_task(task_id)
    if task.state != "verified":
        raise transitions.TransitionError(f"transition not allowed: integrate from {task.state}")
    if slots.checked_out_elsewhere(repo, INTEGRATION) is not None:
        return "held"   # PD-13: never move a branch somebody has checked out
    if slots.checked_out_elsewhere(repo, task_branch(task_id)) is not None:
        return "held"   # somebody may be committing on it: only the verified commit may be integrated (§5 step 8)
    limits = transitions.Limits.from_cfg(task, cfg)
    tip = repo.rev(task_branch(task_id))
    old = repo.rev(INTEGRATION)
    if tip is None or old is None:
        raise ArtelError(f"cannot integrate task {task_id}: {task_branch(task_id)} or {INTEGRATION} is missing")
    verified = board.get_meta(_verified_key(task_id))
    if verified is None or tip != verified or not repo.is_ancestor(old, tip):
        # PD-45: anything committed after verify (or no record of a verified commit) is not integrated;
        # a moved integration branch needs a fresh verify on the new merge
        transitions.apply(board, task_id, "ff_impossible", limits)
        board.del_meta(_verified_key(task_id))
        return "reverify"
    if repo.git("update-ref", _INTEGRATION_REF, tip, old, check=False).returncode != 0:
        return "retry"   # integration moved between the read and the update: nothing changed
    tr = transitions.apply(board, task_id, "ff_ok", limits)
    board.del_meta(_verified_key(task_id))
    text = f"integrated {tip[:12]}"
    usage = _usage_summary(board, task_id)
    if usage:
        text += f" usage: {usage}"
    journal.append(board, "DONE", journal.truncate_utf8(text, journal.TEXT_LIMIT), author=_RUNNER, task_id=task_id)
    slots.apply_git_effects(repo, board, cfg, task_id, tr.effects)
    _record_base(repo, board, tip, ids=testguard.base_ids(repo, tip), log=_last_verify_log(repo, board, task_id))
    return "done"


# --- base moved ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BaseUpdate:
    kind: str            # "unchanged" | "fast_forward" | "merged" | "conflict" | "red" | "timeout" | "stopped"
    old: str
    new: str | None
    log_path: Path | None
    detail: str
    ids: frozenset[str] | None = None   # passed test ids of a green base verify (PD-64)


def base_needs_update(repo: Repo, cfg) -> bool:
    base, integration = repo.rev(cfg["base"]), repo.rev(INTEGRATION)
    return base is not None and integration is not None and not repo.is_ancestor(base, integration)


def _base_log(repo: Repo, kind: str) -> Path:
    directory = repo.logs_dir / "base"
    existing = list(directory.glob(f"{kind}-*.log")) if directory.is_dir() else []
    return directory / f"{kind}-{len(existing) + 1}.log"   # PD-34: no runs row; n = 1 + existing files


def base_update(repo: Repo, board, cfg, slot_n: int, *, on_start: OnStart | None = None, stop_event=None,
                env: Mapping[str, str] | None = None) -> BaseUpdate:
    base = cfg["base"]
    old, base_tip = repo.rev(INTEGRATION), repo.rev(base)
    if old is None or base_tip is None:
        raise ArtelError(f"cannot update from base {base!r}: {INTEGRATION} or the base branch is missing")
    if repo.is_ancestor(base_tip, old):
        return BaseUpdate("unchanged", old, None, None, "")
    if repo.is_ancestor(old, base_tip):
        return BaseUpdate("fast_forward", old, base_tip, None, "")
    slots.prepare_detached(repo, board, cfg, slot_n)
    path = slots.slot_path(repo, cfg, slot_n)
    log_path = _base_log(repo, "verify")
    try:
        files = _merge_into_slot(repo, path, base_tip)
        if files is not None:
            return BaseUpdate("conflict", old, None, None, "conflict in " + ", ".join(files))
        setup = slots.run_setup_if_needed(repo, board, cfg, slot_n, path, task_id=None, on_start=on_start,
                                          stop_event=stop_event, env=env)
        if setup == "stopped":
            return BaseUpdate("stopped", old, None, None, "stopped during setup")
        if setup in ("failed", "timeout"):
            setup_logs = sorted((repo.logs_dir / "base").glob("setup-*.log"), key=lambda p: p.stat().st_mtime)
            return BaseUpdate("timeout" if setup == "timeout" else "red", old, None,
                              setup_logs[-1] if setup_logs else None, f"setup {setup}")
        new = slots._git(repo, "rev-parse", "HEAD", cwd=path).stdout.strip()
        if not cfg["commands.verify"]:
            return BaseUpdate("merged", old, new, None, 'verify not configured (commands.verify="")')
        kind, detail, _ = _verify_command(cfg, repo, path, log_path=log_path, env=env, on_start=on_start,
                                          stop_event=stop_event)
        if kind == "green":
            return BaseUpdate("merged", old, new, log_path, detail,
                              testguard.ids_from_log(log_path, cfg["verify_test_id_regex"]))
        return BaseUpdate("red" if kind == "not_started" else kind, old, None, log_path, detail)
    finally:
        slots.leave_slot(repo, board, cfg, slot_n)


def finish_base_update(repo: Repo, board, cfg, bu: BaseUpdate, *, now: float | None = None) -> str:
    """-> "applied" | "retry" | "held" | "paused" | "noop". `now`: the runner's clock, the `since` of the
    base_conflict pause (T22 review); None = the wall clock."""
    if bu.kind in ("fast_forward", "merged"):
        if slots.checked_out_elsewhere(repo, INTEGRATION) is not None:
            return "held"
        if repo.git("update-ref", _INTEGRATION_REF, bu.new, bu.old, check=False).returncode != 0:
            return "retry"
        merged_base = bu.new if bu.kind == "fast_forward" else repo.rev(f"{bu.new}^2")   # the base tip that was merged
        board.set_meta("last_base", merged_base or bu.new)
        if bu.kind == "merged":   # a verified tree; a fast-forward to the base tip was never verified here
            _record_base(repo, board, bu.new, ids=bu.ids, log=_rel(repo, bu.log_path) if bu.log_path else "",
                         at=now)
        return "applied"
    if bu.kind in ("conflict", "red", "timeout"):
        detail = journal.truncate_utf8(f"{bu.kind}: {bu.detail}", journal.TEXT_LIMIT)
        pauses.add(board, "base_conflict", detail, now=time.time() if now is None else now)
        journal.append(board, "FAIL", journal.truncate_utf8(f"base {cfg['base']} into {INTEGRATION}: {detail}",
                                                            journal.TEXT_LIMIT), author=_RUNNER,
                       basis=journal.truncate_utf8(_rel(repo, bu.log_path) if bu.log_path else "", 200))
        return "paused"
    return "noop"
