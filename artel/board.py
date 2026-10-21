"""SQLite board: tasks, deps, runs, slots, requests, meta (spec §3, §3.1, §8).

One `Board` per thread. Every write goes through `tx()` (BEGIN IMMEDIATE). The
runner lock is `platform.FileLock`; this module has no lock of its own.
"""
import dataclasses
import json
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from artel.errors import ArtelError

SCHEMA_VERSION = 1
STATES = ("proposed", "vetting", "awaiting_approval", "open", "attacking", "writing", "reviewing",
          "fixing", "verifying", "verified", "blocked", "awaiting_accept", "done", "rejected", "stuck")
REQUEST_COMMANDS = ("answer", "approve", "reject", "accept", "rework", "pause", "resume", "stop")


class BoardBusy(ArtelError):
    """sqlite 'database is locked' after busy_timeout."""


class UnknownTask(ArtelError):
    pass


class UnknownSlot(ArtelError):
    pass


class UnknownRequest(ArtelError):
    pass


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    body: str
    area: tuple[str, ...]
    priority: int
    state: str
    created_by: str                 # "human" or "agent:<task id>"
    attack: bool
    strong: bool
    review_rounds: int | None
    merge: str | None
    extra_review_rounds: int
    extra_verify_fixes: int
    attempts: int
    round: int
    verify_fixes: int
    fix_kind: str | None            # "review" | "verify" | "conflict" | None
    asked_by: str | None            # "vet" | "attack" | "write" | "review" | "fix" | "verify" | None
    cause: str | None               # for blocked: "ask" | "cap" | None
    then: str | None                # after a vet-triggered attack: "awaiting_approval" | "open" | None
    rejected_by: str | None         # "vet" | "human" | None
    stuck_from: str | None          # stage state the task got stuck in (PD-28)
    review_closure: int             # next review's template: 0 none, 1 conflict closure (PD-29), 2 verify-fix review (PD-48)
    fix_base: str | None            # task-branch tip when the task last entered fixing (PD-35)
    accepted: bool                  # human accepted the verified commit (PD-36)
    risky: bool                     # vet found the task risky (PD-37)
    answered: bool
    last_verify: str | None         # "green" | "green_flaky" | "red" | "conflict" | "timeout" | None
    running: bool
    running_kind: str | None        # "agent" | "setup" | "verify" (the base job is not a task, PD-34)
    running_started_at: float | None
    running_pid: int | None
    running_group: int | None
    running_fingerprint: str | None
    running_slot: int | None
    version: int
    created_at: float
    updated_at: float
    stage_runs: int = 0             # agent runs since a human last moved the task (PD-58, spec §3.1 "Counters")
    flaky: bool = False             # the last verify was green only on a rerun (R31c)
    resets: int = 0                 # branch resets by a failed fix (PD-63)
    red_seen: bool = False          # an earlier verify of this task was red: it must now be green on every run (PD-72)


@dataclass(frozen=True)
class Slot:
    n: int
    path: str
    task_id: int | None
    setup_hash: str | None


@dataclass(frozen=True)
class Run:
    id: int
    task_id: int
    stage: str
    n: int
    round: int
    model: str | None
    model_reason: str | None
    slot: int | None
    log_path: str
    started_at: float
    ended_at: float | None
    outcome: str | None
    usage: dict
    exit_code: int | None
    timed_out: bool
    killed: bool
    first_context: int | None       # input + cache tokens of the first/last call (PD-23)
    last_context: int | None
    turns: int | None
    duration_ms: int | None
    cost_usd: float | None
    session_id: str | None
    workers: int | None = None      # the runner's worker count when the run started (PD-71 a)
    queued_at: float | None = None  # when a verify run first became due (PD-71 c)


@dataclass(frozen=True)
class IdleSpan:
    slot: int
    reason: str
    since: float
    until: float | None


@dataclass(frozen=True)
class Request:
    id: int
    ts: float
    command: str
    task_id: int | None
    payload: dict
    applied_at: float | None
    error: str | None


_TASK_FIELDS = tuple(f.name for f in dataclasses.fields(Task))
_TASK_BOOLS = frozenset({"attack", "strong", "accepted", "risky", "answered", "running", "flaky", "red_seen"})
_NOT_UPDATABLE = frozenset({"id", "version", "created_at", "updated_at"})
_RUNNING_COLUMNS = ("running_kind", "running_started_at", "running_pid", "running_group",
                    "running_fingerprint", "running_slot")

_IDLE_TABLE = """CREATE TABLE IF NOT EXISTS idle (
    id INTEGER PRIMARY KEY, slot INTEGER NOT NULL, reason TEXT NOT NULL, since REAL NOT NULL, until REAL)"""

# "then" is an SQLite keyword: every column name is double-quoted in generated SQL.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '[]', priority INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
    created_by TEXT NOT NULL, attack INTEGER NOT NULL DEFAULT 0, strong INTEGER NOT NULL DEFAULT 0,
    review_rounds INTEGER, merge TEXT,
    extra_review_rounds INTEGER NOT NULL DEFAULT 0, extra_verify_fixes INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0, round INTEGER NOT NULL DEFAULT 0,
    verify_fixes INTEGER NOT NULL DEFAULT 0, fix_kind TEXT, asked_by TEXT, cause TEXT,
    "then" TEXT, rejected_by TEXT, stuck_from TEXT, review_closure INTEGER NOT NULL DEFAULT 0,
    fix_base TEXT, accepted INTEGER NOT NULL DEFAULT 0, risky INTEGER NOT NULL DEFAULT 0,
    answered INTEGER NOT NULL DEFAULT 0, last_verify TEXT,
    running INTEGER NOT NULL DEFAULT 0, running_kind TEXT, running_started_at REAL, running_pid INTEGER,
    running_group INTEGER, running_fingerprint TEXT, running_slot INTEGER,
    version INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    stage_runs INTEGER NOT NULL DEFAULT 0, flaky INTEGER NOT NULL DEFAULT 0, resets INTEGER NOT NULL DEFAULT 0,
    red_seen INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS task_deps (task_id INTEGER NOT NULL, after_id INTEGER NOT NULL,
    PRIMARY KEY (task_id, after_id))""",
    """CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
    task_id INTEGER, stage TEXT, round INTEGER, text TEXT NOT NULL, basis TEXT NOT NULL DEFAULT '',
    severity TEXT, status TEXT, ref INTEGER, author TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS entries_task ON entries(task_id, id)",
    """CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, stage TEXT NOT NULL,
    n INTEGER NOT NULL, round INTEGER NOT NULL, model TEXT, model_reason TEXT, slot INTEGER,
    log_path TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, outcome TEXT,
    usage TEXT NOT NULL DEFAULT '{}', exit_code INTEGER, timed_out INTEGER NOT NULL DEFAULT 0,
    killed INTEGER NOT NULL DEFAULT 0, first_context INTEGER, last_context INTEGER, turns INTEGER,
    duration_ms INTEGER, cost_usd REAL, session_id TEXT, workers INTEGER, queued_at REAL)""",
    "CREATE TABLE IF NOT EXISTS slots (n INTEGER PRIMARY KEY, path TEXT NOT NULL, task_id INTEGER, setup_hash TEXT)",
    """CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, command TEXT NOT NULL,
    task_id INTEGER, payload TEXT NOT NULL DEFAULT '{}', applied_at REAL, error TEXT)""",
    _IDLE_TABLE,
)

# Columns added after the first release (PD-58): an older board gets them when it is opened.
_ADDED_COLUMNS = (
    ("tasks", "stage_runs", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "flaky", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "resets", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "red_seen", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "workers", "INTEGER"),
    ("runs", "queued_at", "REAL"),
)


def _task_from_row(row: sqlite3.Row) -> Task:
    values = {name: row[name] for name in _TASK_FIELDS}
    values["area"] = tuple(json.loads(values["area"]))
    for name in _TASK_BOOLS:
        values[name] = bool(values[name])
    return Task(**values)


def _run_from_row(row: sqlite3.Row) -> Run:
    values = {f.name: row[f.name] for f in dataclasses.fields(Run)}
    values["usage"] = json.loads(values["usage"])
    values["timed_out"] = bool(values["timed_out"])
    values["killed"] = bool(values["killed"])
    return Run(**values)


def _request_from_row(row: sqlite3.Row) -> Request:
    values = {f.name: row[f.name] for f in dataclasses.fields(Request)}
    values["payload"] = json.loads(values["payload"])
    return Request(**values)


def open_board(path: Path, *, busy_timeout_ms: int = 5000, factory=sqlite3.Connection) -> "Board":
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000, isolation_level=None,
                           factory=factory, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        # The journal-mode switch and the first schema creation can return SQLITE_BUSY without consulting
        # the busy handler (it would deadlock), so concurrent first opens retry until the same deadline.
        deadline = time.monotonic() + busy_timeout_ms / 1000
        while True:
            try:
                board = Board(conn, _choose_journal_mode(conn))
                board._init_schema()
                break
            except (BoardBusy, sqlite3.OperationalError) as e:
                if isinstance(e, sqlite3.OperationalError) and "locked" not in str(e):
                    raise
                if time.monotonic() >= deadline:
                    raise BoardBusy(str(e)) from e
                time.sleep(0.01)
    except BaseException:
        conn.close()   # a leaked handle keeps the db and -wal/-shm files open (Windows cleanup)
        raise
    return board


def _choose_journal_mode(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if row is not None and str(row[0]).lower() == "wal":
            return "wal"
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            raise BoardBusy(str(e)) from e
    try:
        row = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            raise BoardBusy(str(e)) from e
        raise
    mode = str(row[0]).lower()
    if mode != "delete":
        raise ArtelError(f"cannot use board journal mode {mode!r}")
    return mode


class Board:
    def __init__(self, conn: sqlite3.Connection, journal_mode: str):
        self.conn = conn
        self.journal_mode = journal_mode
        self._tx_depth = 0

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE … COMMIT / ROLLBACK. Re-entrant: an inner call joins the outer transaction
        (no savepoint: an exception caught by the caller between the two does not undo the inner writes)."""
        if self._tx_depth:
            self._tx_depth += 1
            try:
                yield self.conn
            finally:
                self._tx_depth -= 1
            return
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            if "locked" in str(e):
                raise BoardBusy(str(e)) from e
            raise
        self._tx_depth = 1
        try:
            yield self.conn
        except BaseException:
            self._tx_depth = 0
            self._rollback()
            raise
        else:
            self._tx_depth = 0
            try:
                self.conn.execute("COMMIT")
            except sqlite3.OperationalError as e:
                self._rollback()
                if "locked" in str(e):
                    raise BoardBusy(str(e)) from e
                raise

    def _rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass   # never mask the original exception

    def _init_schema(self) -> None:
        try:   # read-only opens must not queue behind the runner's write transaction
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.OperationalError as e:
            if "locked" in str(e):
                raise BoardBusy(str(e)) from e
            if "no such table" not in str(e):
                raise
            row = None
        if row is not None:
            self._check_schema_version(row[0])
            self._migrate()
            return
        with self.tx() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
            found = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
            self._check_schema_version(found)

    def _missing_additions(self) -> list[tuple[str, str, str]]:
        have: dict[str, set[str]] = {}
        for table in {t for t, _, _ in _ADDED_COLUMNS}:
            have[table] = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        return [(t, c, ddl) for t, c, ddl in _ADDED_COLUMNS if c not in have[t]]

    def _migrate(self) -> None:
        """PD-58: `ALTER TABLE ... ADD COLUMN` for what an older board lacks, and the `idle` table. A current
        board is only read here: no write lock is taken (read-only commands must not queue behind the runner)."""
        has_idle = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'idle'").fetchone()
        if has_idle is not None and not self._missing_additions():
            return
        with self.tx() as conn:
            for table, column, ddl in self._missing_additions():   # again inside the lock: another opener may have won
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            conn.execute(_IDLE_TABLE)

    @staticmethod
    def _check_schema_version(found: str) -> None:
        if found != str(SCHEMA_VERSION):
            raise ArtelError(f"board schema version {found} is not supported (this artel: {SCHEMA_VERSION})")

    # tasks ---------------------------------------------------------------

    def insert_task(self, *, title: str, state: str, created_by: str, body: str = "",
                    area: Sequence[str] = (), priority: int = 0, attack: bool = False,
                    strong: bool = False, review_rounds: int | None = None,
                    merge: str | None = None, after: Sequence[int] = ()) -> int:
        if state not in STATES:
            raise ValueError(f"unknown task state: {state!r}")
        if isinstance(area, str):
            raise ValueError("area must be a sequence of paths, not a string")
        now = time.time()
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (title, body, area, priority, state, created_by, attack, strong,"
                " review_rounds, merge, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (title, body, json.dumps(list(area)), priority, state, created_by, int(attack), int(strong),
                 review_rounds, merge, now, now))
            task_id = cur.lastrowid
            for after_id in after:
                conn.execute("INSERT OR IGNORE INTO task_deps (task_id, after_id) VALUES (?, ?)",
                             (task_id, after_id))
        return task_id

    def get_task(self, task_id: int) -> Task:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise UnknownTask(f"unknown task: {task_id}")
        return _task_from_row(row)

    def list_tasks(self, states: Iterable[str] | None = None) -> list[Task]:
        rows = self.conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        if isinstance(states, str):
            raise ValueError("states must be an iterable of state names, not a string")
        wanted = None if states is None else set(states)
        return [t for t in map(_task_from_row, rows) if wanted is None or t.state in wanted]

    def update_task(self, task_id: int, *, expect_state: str, expect_version: int, **fields) -> bool:
        """Compare-and-set; False when the state or version moved. Bumps version and updated_at."""
        for name in fields:
            if name not in _TASK_FIELDS or name in _NOT_UPDATABLE:
                raise ValueError(f"cannot update task column: {name!r}")
        if "state" in fields and fields["state"] not in STATES:
            raise ValueError(f"unknown task state: {fields['state']!r}")
        values = []
        for name, value in fields.items():
            if name == "area":
                value = json.dumps(list(value))
            elif name in _TASK_BOOLS:
                value = int(value)
            values.append(value)
        assignments = [f'"{name}" = ?' for name in fields] + ['"version" = "version" + 1', '"updated_at" = ?']
        with self.tx() as conn:
            cur = conn.execute(
                f'UPDATE tasks SET {", ".join(assignments)} WHERE id = ? AND state = ? AND version = ?',
                (*values, time.time(), task_id, expect_state, expect_version))
            return cur.rowcount == 1

    def set_red_seen(self, task_id: int, *, expect_state: str) -> bool:
        """red_seen = 1 (PD-72) while the task is in `expect_state`. Does not bump `version`: a verify job writes
        it between its start and its result, and the runner reads an unchanged version as "nothing applied yet"."""
        with self.tx() as conn:
            cur = conn.execute("UPDATE tasks SET red_seen = 1 WHERE id = ? AND state = ?", (task_id, expect_state))
            return cur.rowcount == 1

    def set_running(self, task_id: int, *, kind: str, pid: int, group: int,
                    fingerprint: str | None, slot: int | None) -> None:
        """Marks a process as running. Does not bump `version` or touch state and counters."""
        now = time.time()
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE tasks SET running = 1, running_kind = ?, running_started_at = ?, running_pid = ?,"
                " running_group = ?, running_fingerprint = ?, running_slot = ?, updated_at = ? WHERE id = ?",
                (kind, now, pid, group, fingerprint, slot, now, task_id))
            if cur.rowcount != 1:
                raise UnknownTask(f"unknown task: {task_id}")

    def clear_running(self, task_id: int) -> None:
        """Requeue: clears the running_* columns only, no counter."""
        columns = ", ".join(f"{c} = NULL" for c in _RUNNING_COLUMNS)
        with self.tx() as conn:
            cur = conn.execute(f"UPDATE tasks SET running = 0, {columns}, updated_at = ? WHERE id = ?",
                               (time.time(), task_id))
            if cur.rowcount != 1:
                raise UnknownTask(f"unknown task: {task_id}")

    def deps(self, task_id: int) -> list[int]:
        rows = self.conn.execute("SELECT after_id FROM task_deps WHERE task_id = ? ORDER BY after_id",
                                 (task_id,)).fetchall()
        return [r[0] for r in rows]

    def all_deps(self) -> dict[int, list[int]]:
        result: dict[int, list[int]] = {r[0]: [] for r in self.conn.execute("SELECT id FROM tasks ORDER BY id")}
        for task_id, after_id in self.conn.execute("SELECT task_id, after_id FROM task_deps ORDER BY after_id"):
            result.setdefault(task_id, []).append(after_id)
        return result

    # meta ----------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)"
                         " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def del_meta(self, key: str) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    # slots ---------------------------------------------------------------

    def upsert_slot(self, n: int, path: str) -> None:
        with self.tx() as conn:
            conn.execute("INSERT INTO slots(n, path) VALUES (?, ?)"
                         " ON CONFLICT(n) DO UPDATE SET path = excluded.path", (n, path))

    def list_slots(self) -> list[Slot]:
        rows = self.conn.execute("SELECT n, path, task_id, setup_hash FROM slots ORDER BY n").fetchall()
        return [Slot(*tuple(r)) for r in rows]

    def _update_slot(self, n: int, column: str, value) -> None:
        with self.tx() as conn:
            cur = conn.execute(f"UPDATE slots SET {column} = ? WHERE n = ?", (value, n))
            if cur.rowcount != 1:
                raise UnknownSlot(f"unknown slot: {n}")

    def set_slot_task(self, n: int, task_id: int | None) -> None:
        self._update_slot(n, "task_id", task_id)

    def set_slot_setup_hash(self, n: int, setup_hash: str | None) -> None:
        self._update_slot(n, "setup_hash", setup_hash)

    # runs ----------------------------------------------------------------

    def start_run(self, *, task_id: int, stage: str, round: int, model: str | None,
                  model_reason: str | None, slot: int | None, log_path: str, workers: int | None = None,
                  queued_at: float | None = None) -> Run:
        """n = 1 + count of existing runs for (task_id, stage)."""
        with self.tx() as conn:
            count = conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = ? AND stage = ?",
                                 (task_id, stage)).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO runs (task_id, stage, n, round, model, model_reason, slot, log_path, started_at,"
                " workers, queued_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, stage, count + 1, round, model, model_reason, slot, log_path, time.time(), workers,
                 queued_at))
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _run_from_row(row)

    def finish_run(self, run_id: int, *, outcome: str, usage: dict, exit_code: int | None,
                   timed_out: bool, killed: bool, first_context: int | None = None,
                   last_context: int | None = None, turns: int | None = None,
                   duration_ms: int | None = None, cost_usd: float | None = None,
                   session_id: str | None = None) -> None:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE runs SET ended_at = ?, outcome = ?, usage = ?, exit_code = ?, timed_out = ?, killed = ?,"
                " first_context = ?, last_context = ?, turns = ?, duration_ms = ?, cost_usd = ?, session_id = ?"
                " WHERE id = ?",
                (time.time(), outcome, json.dumps(usage), exit_code, int(timed_out), int(killed), first_context,
                 last_context, turns, duration_ms, cost_usd, session_id, run_id))
            if cur.rowcount != 1:
                raise ArtelError(f"unknown run: {run_id}")

    def list_runs(self, task_id: int) -> list[Run]:
        rows = self.conn.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [_run_from_row(r) for r in rows]

    def all_runs(self, *, stages: Iterable[str] | None = None) -> list[Run]:
        if isinstance(stages, str):
            raise ValueError("stages must be an iterable of stage names, not a string")
        rows = self.conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
        wanted = None if stages is None else set(stages)
        return [r for r in map(_run_from_row, rows) if wanted is None or r.stage in wanted]

    # idle spans (PD-71 b) ---------------------------------------------------

    def open_idle(self, slot: int, reason: str, now: float) -> int:
        with self.tx() as conn:
            conn.execute("UPDATE idle SET until = ? WHERE slot = ? AND until IS NULL", (now, slot))   # one open span
            return conn.execute("INSERT INTO idle (slot, reason, since) VALUES (?,?,?)",
                                (slot, reason, now)).lastrowid

    def close_idle(self, slot: int, now: float) -> None:
        """Closes the slot's open span, if any."""
        with self.tx() as conn:
            conn.execute("UPDATE idle SET until = ? WHERE slot = ? AND until IS NULL", (now, slot))

    def idle_spans(self, *, since: float | None = None) -> list[IdleSpan]:
        """Spans that overlap the time from `since` on (all when None), oldest first."""
        rows = self.conn.execute("SELECT slot, reason, since, until FROM idle"
                                 " WHERE (? IS NULL OR until IS NULL OR until >= ?) ORDER BY id",
                                 (since, since)).fetchall()
        return [IdleSpan(*tuple(r)) for r in rows]

    # requests (§3.1: the single writer applies them) ------------------------

    def add_request(self, command: str, task_id: int | None, payload: dict) -> int:
        if command not in REQUEST_COMMANDS:
            raise ValueError(f"unknown request command: {command!r}")
        with self.tx() as conn:
            cur = conn.execute("INSERT INTO requests (ts, command, task_id, payload) VALUES (?,?,?,?)",
                               (time.time(), command, task_id, json.dumps(payload)))
            return cur.lastrowid

    def pending_requests(self) -> list[Request]:
        rows = self.conn.execute("SELECT * FROM requests WHERE applied_at IS NULL ORDER BY id").fetchall()
        return [_request_from_row(r) for r in rows]

    def finish_request(self, request_id: int, error: str | None = None) -> None:
        with self.tx() as conn:
            cur = conn.execute("UPDATE requests SET applied_at = ?, error = ? WHERE id = ? AND applied_at IS NULL",
                               (time.time(), error, request_id))
            if cur.rowcount != 1:
                self.get_request(request_id)   # unknown id raises UnknownRequest
                raise ArtelError(f"request {request_id} is already finished")

    def get_request(self, request_id: int) -> Request:
        row = self.conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise UnknownRequest(f"unknown request: {request_id}")
        return _request_from_row(row)
