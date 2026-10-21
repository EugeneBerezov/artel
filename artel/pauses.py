"""The pause set (spec §3.1 "Pauses", PD-54): the only module that reads or writes the pause meta keys.

Board meta `pauses` = JSON object kind -> {"since", "causes": [{"detail", "setting", "until"}], "exhausted"}.
Each kind ends on its own; a kind that ends by itself never removes another kind.
"""
import json
from dataclasses import dataclass

KINDS = ("manual", "base_conflict", "integration_moved", "failures", "limit", "agent_unavailable", "quota",
         "disk", "setup_failed")
HUMAN_KINDS = ("manual", "base_conflict", "integration_moved", "failures")   # plain resume removes them (PD-60)
PROBED_KINDS = ("limit", "agent_unavailable", "quota")      # probed when the pause has no known end (PD-60)
TOKEN_FREE_KINDS = ("limit", "quota", "agent_unavailable")  # verify and integration continue under these only

_KEY = "pauses"
_PROBE_TIMES = "probe_times"
_LAST_PROBE = "last_probe"
PROBE_WINDOW = 24 * 3600.0         # probes are counted within this long (limit_probes, PD-55)
_LEGACY_KEYS = ("pause_reason", "pause_detail", "pause_until")


@dataclass(frozen=True)
class Cause:
    detail: str
    setting: str | None = None
    until: float | None = None


@dataclass(frozen=True)
class Pause:
    kind: str
    since: float
    causes: tuple[Cause, ...]
    exhausted: bool = False

    @property
    def until(self) -> float | None:
        """None if any cause has no known end (it is probed, PD-55), else the latest cause end."""
        if not self.causes or any(c.until is None for c in self.causes):
            return None
        return max(c.until for c in self.causes)

    @property
    def detail(self) -> str:
        return "; ".join(c.detail for c in self.causes)


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown pause kind: {kind!r}")


def _load(board) -> dict:
    raw = board.get_meta(_KEY)
    return json.loads(raw) if raw else {}


def _store(board, records: dict) -> None:
    if records:
        board.set_meta(_KEY, json.dumps(records, sort_keys=True))
    else:
        board.del_meta(_KEY)


def _pause(kind: str, record: dict) -> Pause:
    causes = tuple(Cause(c["detail"], c.get("setting"), c.get("until")) for c in record.get("causes", ()))
    return Pause(kind, record["since"], causes, bool(record.get("exhausted", False)))


def _ordered(records: dict) -> list[str]:
    """KINDS order; a kind this version does not know (a newer artel wrote it) comes last and still counts."""
    return [k for k in KINDS if k in records] + sorted(k for k in records if k not in KINDS)


def active(board) -> dict[str, Pause]:
    records = _load(board)
    return {kind: _pause(kind, records[kind]) for kind in _ordered(records)}


def add(board, kind: str, detail: str, *, now: float, until: float | None = None,
        setting: str | None = None, since: float | None = None) -> bool:
    """True when the kind is newly added (the caller notifies `paused`). On an active kind the cause is appended
    (a cause with the same detail and setting is replaced); `since` and `exhausted` stay (PD-54). `since` is for
    the one caller that re-adds a kind it lifted only to retry (an outage that goes on keeps its start)."""
    _check_kind(kind)
    cause = {"detail": detail, "setting": setting, "until": until}
    with board.tx():
        records = _load(board)
        record = records.get(kind)
        new = record is None
        if new:
            record = records[kind] = {"since": now if since is None else since, "causes": [], "exhausted": False}
        record["causes"] = [c for c in record["causes"]
                            if (c["detail"], c.get("setting")) != (detail, setting)] + [cause]
        _store(board, records)
    return new


def drop_cause(board, kind: str, detail: str) -> str:
    """Removes the causes of `kind` with this detail (a window that fell below its threshold). -> "none" when there
    was no such cause, "dropped", or "emptied" when it was the last one and the kind is gone (the caller notifies
    `resumed`)."""
    _check_kind(kind)
    with board.tx():
        records = _load(board)
        record = records.get(kind)
        if record is None:
            return "none"
        left = [c for c in record["causes"] if c["detail"] != detail]
        if len(left) == len(record["causes"]):
            return "none"
        if left:
            record["causes"] = left
        else:
            del records[kind]
        _store(board, records)
    return "dropped" if left else "emptied"


def set_exhausted(board, kind: str) -> None:
    _check_kind(kind)
    with board.tx():
        records = _load(board)
        if kind in records:
            records[kind]["exhausted"] = True
            _store(board, records)


def remove(board, kind: str) -> bool:
    """True when the kind was active (the caller notifies `resumed`)."""
    _check_kind(kind)
    with board.tx():
        records = _load(board)
        if kind not in records:
            return False
        del records[kind]
        _store(board, records)
    return True


def resume(board, *, force: bool) -> tuple[list[str], str | None]:
    """Removes the human-ended kinds and any kind whose probes are spent (`exhausted`); `force` removes all.
    -> (removed kinds, an error naming the first self-ending kind left, or None)."""
    with board.tx():
        records = _load(board)
        removed = [kind for kind in _ordered(records)
                   if force or kind in HUMAN_KINDS or records[kind].get("exhausted")]
        left = [kind for kind in _ordered(records) if kind not in removed]
        for kind in removed:
            del records[kind]
        _store(board, records)
        if "failures" in removed:   # PD-58, PD-61 amended: both streaks start again
            board.set_meta("failure_streak", json.dumps({"count": 0, "tasks": []}))
            board.set_meta("unavailable_streak", json.dumps({"signature": "", "count": 0}))
    error = f"pause {left[0]} ends by itself; use resume --force" if left else None
    return removed, error


def probe_times(board, *, now: float) -> list[tuple[float, str]]:
    """[ts, kind] of every probe within 24 h, across all kinds (PD-54): kept when a pause is removed, so a pause
    added again soon after another does not get a fresh allowance."""
    raw = board.get_meta(_PROBE_TIMES)
    try:
        pairs = [(float(ts), str(kind)) for ts, kind in (json.loads(raw) if raw else [])]
    except (ValueError, TypeError):
        pairs = []   # an unreadable record counts nothing; the next probe rewrites it
    return [(ts, kind) for ts, kind in pairs if ts > now - PROBE_WINDOW]


def record_probe(board, *, now: float, kind: str, outcome: str) -> None:
    with board.tx():
        pairs = probe_times(board, now=now) + [(now, kind)]
        board.set_meta(_PROBE_TIMES, json.dumps([[ts, k] for ts, k in pairs]))
        board.set_meta(_LAST_PROBE, json.dumps({"at": now, "kind": kind, "outcome": outcome}))


def blocks_agents(board) -> bool:
    return bool(_load(board))


def blocks_verify(board) -> bool:
    return any(kind not in TOKEN_FREE_KINDS for kind in _load(board))


def migrate_legacy(board, *, now: float) -> None:
    """pause_reason / pause_detail / pause_until (Tasks 14, 15) -> one Pause; the legacy keys are deleted."""
    with board.tx():
        reason, detail, until = (board.get_meta(k) for k in _LEGACY_KEYS)
        if reason:
            kind = reason if reason in KINDS else "manual"
            try:
                end = float(until) if until else None
            except ValueError:
                end = None   # unreadable: an unknown end, probed or resumed like any other
            add(board, kind, detail or reason, now=now, until=end)
        for key in _LEGACY_KEYS:
            board.del_meta(key)
