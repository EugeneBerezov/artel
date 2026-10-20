"""Journal entries (spec §3.2): typed, byte-limited, append-only.

No code path updates or deletes an `entries` row: a closure or a correction is a new entry that
references the old one.
"""
import time
from collections.abc import Sequence
from dataclasses import dataclass

from artel.errors import ArtelError

KINDS = ("FACT", "FAIL", "FINDING", "ASK", "ANSWER", "VERDICT", "DONE")
SEVERITIES = ("blocker", "major", "minor")
STATUSES = ("open", "closed")
TEXT_LIMIT = 400
BASIS_LIMIT = 200
_ELLIPSIS = "…"


class EntryTooLong(ArtelError):
    pass


@dataclass(frozen=True)
class Entry:
    id: int
    ts: float
    kind: str
    task_id: int | None
    stage: str | None
    round: int | None
    text: str
    basis: str
    severity: str | None
    status: str | None
    ref: int | None
    author: str


def utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def truncate_utf8(s: str, limit: int) -> str:
    """At most `limit` bytes, never splitting a code point; "…" marks a cut."""
    if utf8_len(s) <= limit:
        return s
    marker = utf8_len(_ELLIPSIS)
    if limit < marker:
        return s.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    head = s.encode("utf-8")[:limit - marker].decode("utf-8", errors="ignore")
    return head + _ELLIPSIS


def _check_limit(field: str, value: str, limit: int) -> None:
    size = utf8_len(value)
    if size > limit:
        raise EntryTooLong(f"journal {field} is {size} bytes, the limit is {limit} (UTF-8 bytes, not characters);"
                           " commit a file and reference it in basis")


def _from_row(row) -> Entry:
    return Entry(**{name: row[name] for name in Entry.__dataclass_fields__})


def append(board, kind: str, text: str, *, author: str, task_id: int | None = None,
           stage: str | None = None, round: int | None = None, basis: str = "",
           severity: str | None = None, status: str | None = None, ref: int | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"unknown journal kind: {kind!r}")
    if kind != "FINDING" and (severity is not None or status is not None):
        raise ValueError("severity and status belong to FINDING entries only")
    if severity is not None and severity not in SEVERITIES:
        raise ValueError(f"unknown severity: {severity!r}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown finding status: {status!r}")
    if not author:
        raise ValueError("a journal entry needs an author")
    _check_limit("text", text, TEXT_LIMIT)
    _check_limit("basis", basis, BASIS_LIMIT)
    with board.tx() as conn:
        cur = conn.execute(
            "INSERT INTO entries (ts, kind, task_id, stage, round, text, basis, severity, status, ref, author)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), kind, task_id, stage, round, text, basis, severity, status, ref, author))
        return cur.lastrowid


def add_finding(board, *, task_id: int, stage: str, round: int, severity: str, text: str,
                basis: str, author: str) -> int:
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity: {severity!r}")
    return append(board, "FINDING", text, author=author, task_id=task_id, stage=stage, round=round,
                  basis=basis, severity=severity, status="open")


def get_entry(board, entry_id: int) -> Entry:
    row = board.conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise ArtelError(f"unknown journal entry: {entry_id}")
    return _from_row(row)


def _is_closed(conn, finding_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM entries WHERE kind = 'FINDING' AND status = 'closed' AND ref = ?",
                       (finding_id,)).fetchone()
    return row is not None


def close_finding(board, finding_id: int, *, task_id: int, round: int, basis: str, author: str) -> int:
    with board.tx() as conn:
        row = conn.execute("SELECT * FROM entries WHERE id = ?", (finding_id,)).fetchone()
        if (row is None or row["kind"] != "FINDING" or row["status"] != "open" or row["task_id"] != task_id
                or _is_closed(conn, finding_id)):
            raise ArtelError(f"#{finding_id} is not an open finding of task {task_id}")
        return append(board, "FINDING", f"closed #{finding_id}", author=author, task_id=task_id,
                      stage=row["stage"], round=round, basis=basis, severity=row["severity"],
                      status="closed", ref=finding_id)


def correct(board, old_id: int, text: str, *, author: str, basis: str = "") -> int:
    old = get_entry(board, old_id)
    return append(board, old.kind, text, author=author, task_id=old.task_id, stage=old.stage,
                  round=old.round, basis=basis, ref=old_id)


def task_entries(board, task_id: int) -> list[Entry]:
    rows = board.conn.execute("SELECT * FROM entries WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
    return [_from_row(r) for r in rows]


def findings(board, task_id: int, *, stages: Sequence[str] | None = None) -> list[tuple[Entry, bool]]:
    """(finding, is_open) for the original FINDING entries; a later closing entry that refs one closes it."""
    entries = task_entries(board, task_id)
    closed = {e.ref for e in entries if e.kind == "FINDING" and e.status == "closed"}
    wanted = None if stages is None else set(stages)
    return [(e, e.id not in closed) for e in entries
            if e.kind == "FINDING" and e.status == "open" and (wanted is None or e.stage in wanted)]


def open_findings(board, task_id: int, *, stages: Sequence[str] = ("review",)) -> list[Entry]:
    return [e for e, is_open in findings(board, task_id, stages=stages) if is_open]


def recent_entries(board, *, kinds: Sequence[str] | None = None,
                   exclude_task: int | None = None, limit: int = 500) -> list[Entry]:
    where, args = [], []
    if kinds is not None:
        where.append(f"kind IN ({','.join('?' * len(kinds))})")
        args.extend(kinds)
    if exclude_task is not None:
        where.append("(task_id IS NULL OR task_id != ?)")
        args.append(exclude_task)
    sql = "SELECT * FROM entries" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    rows = board.conn.execute(sql, (*args, limit)).fetchall()
    return [_from_row(r) for r in rows]
