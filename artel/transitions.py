"""The task transition table of spec §3.1 as data, and a pure engine over it.

No git, journal or process code lives here: `decide` is pure, `apply` only reads a task and does one
compare-and-set through the board. Effects are named and returned for the caller to execute."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from artel.board import STATES, Board, Task
from artel.errors import ArtelError


class TransitionError(ArtelError):
    pass


class StaleTask(ArtelError):
    pass


@dataclass(frozen=True)
class Limits:
    max_attempts: int
    review_rounds: int
    max_verify_fixes: int
    merge: str
    autoapprove: str

    @staticmethod
    def for_task(task: Task, *, max_attempts: int, review_rounds: int, max_verify_fixes: int,
                 merge: str, autoapprove: str) -> "Limits":
        return Limits(
            max_attempts=max_attempts,
            review_rounds=(task.review_rounds or review_rounds) + task.extra_review_rounds,
            max_verify_fixes=max_verify_fixes + task.extra_verify_fixes,
            merge=task.merge or merge,
            autoapprove=autoapprove)

    @staticmethod
    def from_cfg(task: Task, cfg: Mapping[str, object]) -> "Limits":
        return Limits.for_task(task, max_attempts=cfg["max_attempts"], review_rounds=cfg["review_rounds"],
                               max_verify_fixes=cfg["max_verify_fixes"], merge=cfg["merge"],
                               autoapprove=cfg["autoapprove"])


@dataclass(frozen=True)
class Row:
    rid: str
    sources: tuple[str, ...]
    event: str
    to: str
    when: tuple[str, ...] = ()
    sets: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    stuck_on_cap: bool = False
    cause: str = ""


@dataclass(frozen=True)
class Transition:
    rid: str
    event: str
    from_state: str | None
    to_state: str
    updates: dict[str, object]
    effects: tuple[str, ...]
    cause: str


EVENTS = ("add_human", "add_agent", "pick", "vet_ok", "vet_risky", "vet_inadequate", "approve",
          "reject", "attack_clear", "attack_blocker", "stage_failed", "ready", "ask",
          "setup_failed", "internal_error", "review_clear", "review_blocking", "answer", "rework",
          "verify_green", "verify_red", "verify_conflict", "accept", "ff_ok", "ff_impossible")
EFFECTS = ("delete_branch", "reset_branch", "terminate_running", "append_findings", "append_answer",
           "ask_open_findings", "pause_setup_failed", "notify_internal_error")

STAGE_STATES = ("vetting", "attacking", "writing", "reviewing", "fixing")
JOB_STATES = STAGE_STATES + ("verifying",)
LIVE = ("proposed", "vetting", "awaiting_approval", "open", "attacking", "writing", "reviewing",
        "fixing", "verifying", "verified", "blocked", "stuck", "awaiting_accept")

TABLE = (
    Row("R01", (), "add_human", "open"),
    Row("R02", (), "add_agent", "proposed"),
    Row("R03", ("proposed",), "pick", "vetting"),
    Row("R04a", ("vetting",), "vet_ok", "open", when=("auto=non_risky|all",), cause="autoapprove"),
    Row("R04b", ("vetting",), "vet_ok", "awaiting_approval", when=("auto=none",), cause="autoapprove"),
    Row("R04c", ("vetting",), "vet_risky", "attacking", when=("auto=none|non_risky",),
        sets=("then=awaiting_approval", "risky=1"), cause="autoapprove"),
    Row("R04d", ("vetting",), "vet_risky", "attacking", when=("auto=all",), sets=("then=open", "risky=1"),
        cause="autoapprove"),
    Row("R05", ("vetting",), "vet_inadequate", "rejected", sets=("rejected_by=vet",)),
    Row("R06", ("vetting",), "stage_failed", "=", sets=("attempts+1",), stuck_on_cap=True,
        cause="max_attempts"),
    Row("R07a", ("attacking",), "attack_clear", "awaiting_approval", when=("then=awaiting_approval",),
        sets=("then=none",), effects=("append_findings",)),
    Row("R07b", ("attacking",), "attack_clear", "open", when=("then=open",), sets=("then=none",),
        effects=("append_findings",)),
    Row("R08", ("proposed", "awaiting_approval", "rejected"), "approve", "open",
        when=("approvable",), sets=("rejected_by=none",)),
    Row("R09", LIVE, "reject", "rejected", sets=("rejected_by=human",),
        effects=("terminate_running", "delete_branch")),
    Row("R10a", ("open",), "pick", "attacking", when=("attack",)),
    Row("R10b", ("open",), "pick", "writing", when=("not_attack",)),
    Row("R11", ("attacking",), "attack_clear", "writing", when=("then=none",), effects=("append_findings",)),
    Row("R12", ("attacking",), "attack_blocker", "blocked", sets=("asked_by=attack", "cause=ask")),
    Row("R13", ("attacking", "reviewing"), "stage_failed", "=", sets=("attempts+1",),
        stuck_on_cap=True, cause="max_attempts"),
    Row("R14a", ("writing",), "ready", "reviewing", sets=("round+1",)),
    Row("R14b", ("fixing",), "ready", "reviewing", when=("fix_kind=review",), sets=("round+1", "fix_kind=none")),
    Row("R15", ("fixing",), "ready", "verifying", when=("fix_kind=verify",), sets=("fix_kind=none",)),
    Row("R16", ("fixing",), "ready", "reviewing", when=("fix_kind=conflict",),
        sets=("fix_kind=none", "review_closure=1")),
    Row("R17", ("writing", "fixing"), "stage_failed", "open",
        sets=("attempts+1", "round=0", "verify_fixes=0", "fix_kind=none", "fix_base=none", "accepted=0"),
        effects=("reset_branch",), stuck_on_cap=True, cause="max_attempts"),
    Row("R18", STAGE_STATES, "ask", "blocked", sets=("asked_by=stage", "cause=ask")),
    Row("R19", JOB_STATES, "setup_failed", "=", effects=("pause_setup_failed",)),
    Row("R20", JOB_STATES, "internal_error", "=", sets=("attempts+1",),
        effects=("notify_internal_error",), stuck_on_cap=True, cause="max_attempts"),
    Row("R21", ("reviewing",), "review_clear", "verifying", sets=("review_closure=0",)),
    Row("R22a", ("reviewing",), "review_blocking", "fixing", when=("round<rounds",),
        sets=("fix_kind=review", "review_closure=0", "fix_base=tip"), cause="review_rounds"),
    Row("R22b", ("reviewing",), "review_blocking", "blocked", when=("round>=rounds",),
        sets=("asked_by=review", "cause=cap", "review_closure=0"), effects=("ask_open_findings",),
        cause="review_rounds"),
    Row("R23", ("blocked",), "answer", "attacking", when=("asked_by=attack",), sets=("clear_ask",),
        effects=("append_answer",)),
    Row("R24", ("blocked",), "answer", "@asked", when=("asked_by=write|fix",), sets=("clear_ask",),
        effects=("append_answer",)),
    Row("R25", ("blocked",), "answer", "reviewing", when=("asked_by=review", "cause=ask"),
        sets=("clear_ask",), effects=("append_answer",)),
    Row("R26", ("blocked",), "answer", "fixing", when=("asked_by=review", "cause=cap"),
        sets=("fix_kind=review", "extra_review_rounds+1", "clear_ask", "fix_base=tip"),
        effects=("append_answer",)),
    Row("R27", ("blocked",), "answer", "vetting", when=("asked_by=vet",), sets=("clear_ask",),
        effects=("append_answer",)),
    Row("R28", ("blocked",), "answer", "fixing", when=("asked_by=verify",),
        sets=("fix_kind=verify", "extra_verify_fixes+1", "clear_ask", "fix_base=tip"),
        effects=("append_answer",)),
    Row("R29a", ("blocked",), "rework", "fixing", when=("has_branch",),
        sets=("fix_kind=review", "extra_review_rounds+1", "clear_ask", "fix_base=tip"),
        effects=("append_answer",)),
    Row("R29b", ("stuck",), "rework", "fixing", when=("has_branch",),
        sets=("fix_kind=review", "extra_review_rounds+1", "attempts=0", "stuck_from=none", "answered=1",
              "fix_base=tip", "accepted=0"),
        effects=("append_answer",)),
    Row("R30", ("stuck",), "rework", "@stuck_from", when=("no_branch",),
        sets=("attempts=0", "stuck_from=none", "answered=1"), effects=("append_answer",)),
    Row("R31a", ("verifying",), "verify_green", "verified", when=("merge=auto|accepted",), cause="merge"),
    Row("R31b", ("verifying",), "verify_green", "awaiting_accept", when=("merge=ask", "not_accepted"),
        cause="merge"),
    Row("R32", ("verifying",), "verify_conflict", "fixing", when=("vfix<cap",),
        sets=("fix_kind=conflict", "verify_fixes+1", "accepted=0"), cause="max_verify_fixes"),
    Row("R33", ("verifying",), "verify_red", "fixing", when=("vfix<cap",),
        sets=("fix_kind=verify", "verify_fixes+1", "accepted=0", "fix_base=tip"), cause="max_verify_fixes"),
    Row("R34a", ("verifying",), "verify_red", "blocked", when=("vfix>=cap",),
        sets=("asked_by=verify", "cause=cap", "accepted=0"), cause="max_verify_fixes"),
    Row("R34b", ("verifying",), "verify_conflict", "blocked", when=("vfix>=cap",),
        sets=("asked_by=verify", "cause=cap", "accepted=0"), cause="max_verify_fixes"),
    Row("R35", ("awaiting_accept",), "accept", "verified", sets=("accepted=1",)),
    Row("R36", ("verified",), "ff_ok", "done", effects=("delete_branch",)),
    Row("R37", ("verified",), "ff_impossible", "verifying"),
)

_STAGE_OF_STATE = {"vetting": "vet", "attacking": "attack", "writing": "write", "reviewing": "review",
                   "fixing": "fix"}
_STATE_OF_STAGE = {"write": "writing", "fix": "fixing"}
_BOOL_COLUMNS = {"accepted", "risky", "review_closure", "answered"}
_INT_COLUMNS = {"attempts", "round", "verify_fixes"}


class _Ctx:
    """Everything a guard or an update may read; limits and the caller's facts are checked on use."""

    def __init__(self, task: Task | None, limits: Limits | None, stage: str | None,
                 has_branch: bool | None, tip: str | None):
        self.task, self._limits, self.stage, self._has_branch, self.tip = task, limits, stage, has_branch, tip

    @property
    def limits(self) -> Limits:
        if self._limits is None:
            raise TransitionError("limits are required for this transition")
        return self._limits

    @property
    def has_branch(self) -> bool:
        if self._has_branch is None:
            raise TransitionError("has_branch is required for this transition")
        return self._has_branch


def _guard(name: str, c: _Ctx) -> bool:
    t = c.task
    if name == "attack":
        return t.attack
    if name == "not_attack":
        return not t.attack
    if name == "approvable":
        return t.state != "rejected" or t.rejected_by == "vet"
    if name == "has_branch":
        return c.has_branch
    if name == "no_branch":
        return not c.has_branch
    if name == "not_accepted":
        return not t.accepted
    if name == "round<rounds":
        return t.round < c.limits.review_rounds
    if name == "round>=rounds":
        return t.round >= c.limits.review_rounds
    if name == "vfix<cap":
        return t.verify_fixes < c.limits.max_verify_fixes
    if name == "vfix>=cap":
        return t.verify_fixes >= c.limits.max_verify_fixes
    key, sep, value = name.partition("=")
    if not sep:
        raise AssertionError(f"unknown guard {name!r}")   # table bug
    options = value.split("|")
    if key == "auto":
        return c.limits.autoapprove in options
    if key == "merge":
        return any(t.accepted if o == "accepted" else c.limits.merge == o for o in options)
    if key in ("fix_kind", "then", "asked_by", "cause"):
        current = getattr(t, key)
        return any((current is None) if o == "none" else current == o for o in options)
    raise AssertionError(f"unknown guard {name!r}")


def _update(spec: str, c: _Ctx) -> dict[str, object]:
    t = c.task
    if spec == "clear_ask":
        return {"asked_by": None, "cause": None, "answered": True}
    if spec.endswith("+1"):
        column = spec[:-2]
        return {column: getattr(t, column) + 1}
    column, _, value = spec.partition("=")
    if column in _BOOL_COLUMNS:
        return {column: bool(int(value))}
    if column in _INT_COLUMNS:
        return {column: int(value)}
    if value == "none":
        return {column: None}
    if value == "tip":
        if c.tip is None:
            raise TransitionError(f"tip is required for {spec}")
        return {column: c.tip}
    if value == "stage":
        if c.stage is None:
            raise TransitionError("stage is required for ask")
        if _STAGE_OF_STATE.get(t.state) != c.stage:
            raise TransitionError(f"stage {c.stage!r} does not match state {t.state!r}")
        return {column: c.stage}
    return {column: value}


def _target(row: Row, c: _Ctx) -> str:
    t = c.task
    if row.to == "=":
        return t.state
    if row.to == "@asked":
        return _STATE_OF_STAGE[t.asked_by]
    if row.to == "@stuck_from":
        if t.stuck_from is None:
            raise TransitionError("task has no stuck_from to return to")
        return t.stuck_from
    return row.to


def _cause(row: Row, c: _Ctx, stuck: bool, attempts: int | None) -> str:
    t = c.task
    if stuck:
        return f"attempts {attempts} >= max_attempts {c.limits.max_attempts}"
    if row.cause == "autoapprove":
        return f"autoapprove={c.limits.autoapprove}"
    if row.cause == "merge":
        return f"merge={c.limits.merge}"
    if row.cause == "review_rounds":
        op = "<" if t.round < c.limits.review_rounds else ">="
        return f"round {t.round} {op} review_rounds {c.limits.review_rounds}"
    if row.cause == "max_verify_fixes":
        op = "<" if t.verify_fixes < c.limits.max_verify_fixes else ">="
        return f"verify_fixes {t.verify_fixes} {op} max_verify_fixes {c.limits.max_verify_fixes}"
    return ""


def matching_rows(task: Task | None, event: str, limits: Limits | None, *, stage: str | None = None,
                  has_branch: bool | None = None, tip: str | None = None) -> list[Row]:
    c = _Ctx(task, limits, stage, has_branch, tip)
    state = None if task is None else task.state
    found = []
    for row in TABLE:
        if row.event != event:
            continue
        if (state is None and row.sources) or (state is not None and state not in row.sources):
            continue
        if state is not None and all(_guard(g, c) for g in row.when):
            found.append(row)
        elif state is None:
            found.append(row)
    return found


def decide(task: Task | None, event: str, limits: Limits | None, *, stage: str | None = None,
           has_branch: bool | None = None, tip: str | None = None) -> Transition:
    if event not in EVENTS:
        raise TransitionError(f"unknown event: {event}")
    found = matching_rows(task, event, limits, stage=stage, has_branch=has_branch, tip=tip)
    state = None if task is None else task.state
    if not found:
        raise TransitionError(f"transition not allowed: {event} from {state}")
    if len(found) > 1:
        raise AssertionError(f"transition table is ambiguous: {event} from {state}: "
                             f"{[r.rid for r in found]}")
    row = found[0]
    if task is None:
        return Transition(row.rid, event, None, row.to, {}, row.effects, "")
    c = _Ctx(task, limits, stage, has_branch, tip)
    updates: dict[str, object] = {}
    for spec in row.sets:
        updates.update(_update(spec, c))
    to = _target(row, c)
    stuck = False
    if row.stuck_on_cap and updates["attempts"] >= c.limits.max_attempts:
        stuck = True
        to = "stuck"
        updates["stuck_from"] = "writing" if row.rid == "R17" else task.state   # R17 reset the branch (PD-28)
    cause = _cause(row, c, stuck, updates.get("attempts")) if row.cause else ""
    return Transition(row.rid, event, task.state, to, updates, row.effects, cause)


def apply(board: Board, task_id: int, event: str, limits: Limits, *, stage: str | None = None,
          has_branch: bool | None = None, tip: str | None = None) -> Transition:
    task = board.get_task(task_id)
    tr = decide(task, event, limits, stage=stage, has_branch=has_branch, tip=tip)
    if not board.update_task(task_id, expect_state=task.state, expect_version=task.version,
                             state=tr.to_state, **tr.updates):
        raise StaleTask(f"task {task_id} changed under us ({event} from {task.state})")
    return tr


def create(board: Board, event: str, **task_fields: Any) -> int:
    tr = decide(None, event, None)
    if event == "add_human":
        task_fields.setdefault("created_by", "human")
    return board.insert_task(state=tr.to_state, **task_fields)


assert all(r.to in STATES or r.to in ("=", "@asked", "@stuck_from") for r in TABLE)
