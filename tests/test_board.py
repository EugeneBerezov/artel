import dataclasses
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from artel import board as B
from tests.factories import make_task


class FailingWal(sqlite3.Connection):
    def execute(self, sql, *args):
        if sql.strip().upper().startswith("PRAGMA JOURNAL_MODE=WAL"):
            raise sqlite3.OperationalError("WAL not supported on this filesystem")
        return super().execute(sql, *args)


class BoardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self._tmp.name) / "artel" / "board.db"
        self.board = B.open_board(self.path)

    def tearDown(self):
        self.board.close()
        self._tmp.cleanup()

    def new_task(self, **kw):
        kw.setdefault("title", "t")
        kw.setdefault("state", "open")
        kw.setdefault("created_by", "human")
        return self.board.insert_task(**kw)

    def test_creates_parent_dir_and_schema_version(self):
        self.assertTrue(self.path.is_file())
        self.assertEqual(self.board.get_meta("schema_version"), "1")
        self.board.set_meta("k", "v")
        again = B.open_board(self.path)   # idempotent: does not wipe data
        self.assertEqual(again.get_meta("schema_version"), "1")
        self.assertEqual(again.get_meta("k"), "v")
        again.close()

    def test_wal_by_default(self):
        self.assertEqual(self.board.journal_mode, "wal")

    def test_wal_fallback_to_delete(self):
        other = B.open_board(Path(self._tmp.name) / "b2.db", factory=FailingWal)
        self.assertEqual(other.journal_mode, "delete")
        row = other.conn.execute("PRAGMA journal_mode").fetchone()
        self.assertEqual(row[0], "delete")
        other.close()

    def test_isolation_level_none(self):
        self.assertIsNone(self.board.conn.isolation_level)

    def test_insert_and_get_roundtrip(self):
        a = self.new_task()
        b = self.new_task(title="second", body="b", area=["src/x", "docs"], priority=3, attack=True,
                          strong=True, review_rounds=2, merge="squash", after=[a])
        t = self.board.get_task(b)
        self.assertEqual((t.title, t.body, t.priority, t.state, t.created_by), ("second", "b", 3, "open", "human"))
        self.assertEqual(t.area, ("src/x", "docs"))
        self.assertIs(t.attack, True)
        self.assertIs(t.strong, True)
        self.assertIs(t.running, False)
        self.assertEqual((t.review_rounds, t.merge), (2, "squash"))
        self.assertEqual(self.board.deps(b), [a])
        self.assertEqual(self.board.deps(a), [])
        self.assertEqual(self.board.all_deps()[b], [a])
        self.assertGreater(t.created_at, 0)

    def test_insert_rejects_unknown_state(self):
        with self.assertRaises(ValueError):
            self.new_task(state="nonsense")

    def test_get_unknown_task_raises(self):
        with self.assertRaises(B.UnknownTask):
            self.board.get_task(99)

    def test_list_tasks_filters_by_state(self):
        a = self.new_task()
        b = self.new_task(state="blocked")
        self.assertEqual([t.id for t in self.board.list_tasks()], [a, b])
        self.assertEqual([t.id for t in self.board.list_tasks(["blocked"])], [b])
        self.assertEqual(self.board.list_tasks([]), [])

    def test_update_task_is_compare_and_set(self):
        tid = self.new_task()
        t = self.board.get_task(tid)
        self.assertTrue(self.board.update_task(tid, expect_state="open", expect_version=t.version, state="writing"))
        self.assertFalse(self.board.update_task(tid, expect_state="open", expect_version=t.version, state="attacking"))
        after = self.board.get_task(tid)
        self.assertEqual(after.state, "writing")
        self.assertEqual(after.version, t.version + 1)
        # right state but stale version
        self.assertFalse(self.board.update_task(tid, expect_state="writing", expect_version=t.version, state="done"))
        # right version but wrong state
        self.assertFalse(self.board.update_task(tid, expect_state="open", expect_version=after.version, state="done"))

    def test_update_task_rejects_unknown_column(self):
        tid = self.new_task()
        t = self.board.get_task(tid)
        with self.assertRaises(ValueError):
            self.board.update_task(tid, expect_state="open", expect_version=t.version, nope=1, title="changed")
        with self.assertRaises(ValueError):
            self.board.update_task(tid, expect_state="open", expect_version=t.version, version=50)
        self.assertEqual(self.board.get_task(tid), t)

    def test_update_task_converts_types(self):
        tid = self.new_task()
        t = self.board.get_task(tid)
        self.board.update_task(tid, expect_state="open", expect_version=t.version,
                               area=("a", "b"), answered=True, attack=True)
        t = self.board.get_task(tid)
        self.assertEqual(t.area, ("a", "b"))
        self.assertIs(t.answered, True)
        self.assertIs(t.attack, True)

    def test_set_and_clear_running_keeps_state_and_counters(self):
        tid = self.new_task()
        t = self.board.get_task(tid)
        self.board.update_task(tid, expect_state="open", expect_version=t.version,
                               state="writing", attempts=2, round=1)
        before = self.board.get_task(tid)
        self.board.set_running(tid, kind="agent", pid=123, group=123, fingerprint="fp", slot=1)
        r = self.board.get_task(tid)
        self.assertIs(r.running, True)
        self.assertEqual((r.running_kind, r.running_pid, r.running_group, r.running_fingerprint, r.running_slot),
                         ("agent", 123, 123, "fp", 1))
        self.assertIsNotNone(r.running_started_at)
        self.assertEqual((r.state, r.attempts, r.round), ("writing", 2, 1))
        self.board.clear_running(tid)
        c = self.board.get_task(tid)
        self.assertIs(c.running, False)
        self.assertEqual((c.running_kind, c.running_pid, c.running_group, c.running_fingerprint,
                          c.running_slot, c.running_started_at), (None,) * 6)
        self.assertEqual((c.state, c.attempts, c.round), ("writing", 2, 1))
        self.assertEqual(c.version, before.version)

    def test_running_on_unknown_task_raises(self):
        with self.assertRaises(B.UnknownTask):
            self.board.set_running(7, kind="agent", pid=1, group=1, fingerprint=None, slot=None)
        with self.assertRaises(B.UnknownTask):
            self.board.clear_running(7)

    def test_tx_rolls_back_on_exception(self):
        with self.assertRaises(RuntimeError):
            with self.board.tx() as conn:
                conn.execute("INSERT INTO meta(key, value) VALUES ('x', '1')")
                raise RuntimeError("boom")
        self.assertIsNone(self.board.get_meta("x"))

    def test_tx_is_reentrant(self):
        tid = self.new_task()
        t = self.board.get_task(tid)
        with self.board.tx():
            self.assertTrue(self.board.update_task(tid, expect_state="open", expect_version=t.version, priority=4))
            self.board.set_meta("k", "v")
        self.assertEqual(self.board.get_task(tid).priority, 4)
        self.assertEqual(self.board.get_meta("k"), "v")

    def test_second_writer_times_out_with_board_busy(self):
        other = B.open_board(self.path, busy_timeout_ms=50)
        with self.board.tx():
            with self.assertRaises(B.BoardBusy):
                with other.tx():
                    pass
        with other.tx():   # usable again once the first writer is gone
            pass
        other.close()

    def test_second_writer_waits_then_succeeds(self):
        entered = threading.Event()
        errors = []

        def hold():
            try:
                b = B.open_board(self.path)
                with b.tx():
                    entered.set()
                    time.sleep(0.3)
                b.close()
            except Exception as e:   # noqa: BLE001 - surfaced below
                errors.append(e)
                entered.set()

        th = threading.Thread(target=hold)
        th.start()
        self.assertTrue(entered.wait(5))
        start = time.monotonic()
        self.board.set_meta("late", "1")
        waited = time.monotonic() - start
        th.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.board.get_meta("late"), "1")
        self.assertGreater(waited, 0.1)

    def test_meta_get_set_del(self):
        self.assertIsNone(self.board.get_meta("a"))
        self.assertEqual(self.board.get_meta("a", "dflt"), "dflt")
        self.board.set_meta("a", "1")
        self.board.set_meta("a", "2")
        self.assertEqual(self.board.get_meta("a"), "2")
        self.board.del_meta("a")
        self.assertIsNone(self.board.get_meta("a"))
        self.board.del_meta("a")   # deleting a missing key is fine

    def test_slots_upsert_and_assign(self):
        tid = self.new_task()
        self.board.upsert_slot(1, "/s/artel-1")
        self.board.upsert_slot(2, "/s/artel-2")
        self.board.set_slot_task(1, tid)
        self.board.set_slot_setup_hash(1, "h")
        self.board.upsert_slot(1, "/s/moved")   # path updated, assignment kept
        slots = self.board.list_slots()
        self.assertEqual([s.n for s in slots], [1, 2])
        self.assertEqual(slots[0], B.Slot(n=1, path="/s/moved", task_id=tid, setup_hash="h"))
        self.assertEqual(slots[1], B.Slot(n=2, path="/s/artel-2", task_id=None, setup_hash=None))
        self.board.set_slot_task(1, None)
        self.board.set_slot_setup_hash(1, None)
        self.assertEqual(self.board.list_slots()[0], B.Slot(n=1, path="/s/moved", task_id=None, setup_hash=None))
        with self.assertRaises(B.ArtelError):
            self.board.set_slot_task(9, tid)

    def test_runs_numbering_per_task_and_stage(self):
        t1, t2 = self.new_task(), self.new_task()

        def start(task, stage):
            return self.board.start_run(task_id=task, stage=stage, round=0, model="m", model_reason="r",
                                        slot=1, log_path=f"logs/{task}/{stage}.jsonl")

        w1, w2, r1, other = start(t1, "write"), start(t1, "write"), start(t1, "review"), start(t2, "write")
        self.assertEqual([w1.n, w2.n, r1.n, other.n], [1, 2, 1, 1])
        self.assertIsNone(w1.ended_at)
        self.board.finish_run(w1.id, outcome="ok", usage={"input_tokens": 5}, exit_code=0,
                              timed_out=False, killed=False)
        done = self.board.list_runs(t1)[0]
        self.assertEqual((done.outcome, done.usage, done.exit_code, done.timed_out, done.killed),
                         ("ok", {"input_tokens": 5}, 0, False, False))
        self.assertIsNotNone(done.ended_at)
        self.assertEqual([r.id for r in self.board.list_runs(t1)], [w1.id, w2.id, r1.id])

    def test_finish_run_stores_measurements(self):
        tid = self.new_task()
        w = self.board.start_run(task_id=tid, stage="write", round=1, model=None, model_reason=None,
                                 slot=None, log_path="l")
        self.board.start_run(task_id=tid, stage="review", round=1, model=None, model_reason=None,
                             slot=None, log_path="l2")
        self.board.finish_run(w.id, outcome="killed", usage={}, exit_code=None, timed_out=True, killed=True,
                              first_context=100, last_context=900, turns=7, duration_ms=1234,
                              cost_usd=0.25, session_id="sess")
        run = self.board.all_runs(stages=["write"])
        self.assertEqual([r.id for r in run], [w.id])
        r = run[0]
        self.assertEqual((r.first_context, r.last_context, r.turns, r.duration_ms, r.cost_usd, r.session_id),
                         (100, 900, 7, 1234, 0.25, "sess"))
        self.assertEqual((r.timed_out, r.killed, r.exit_code), (True, True, None))
        self.assertEqual(len(self.board.all_runs()), 2)

    def test_requests_pending_and_finish(self):
        tid = self.new_task()
        r1 = self.board.add_request("approve", tid, {"why": "ok"})
        r2 = self.board.add_request("pause", None, {})
        self.assertEqual([r.id for r in self.board.pending_requests()], [r1, r2])
        first = self.board.get_request(r1)
        self.assertEqual((first.command, first.task_id, first.payload), ("approve", tid, {"why": "ok"}))
        self.board.finish_request(r1)
        self.board.finish_request(r2, error="bad state")
        self.assertEqual(self.board.pending_requests(), [])
        self.assertIsNotNone(self.board.get_request(r1).applied_at)
        self.assertIsNone(self.board.get_request(r1).error)
        self.assertEqual(self.board.get_request(r2).error, "bad state")
        with self.assertRaises(ValueError):
            self.board.add_request("explode", None, {})
        with self.assertRaises(B.ArtelError):
            self.board.get_request(999)

    def test_new_states_and_fields_roundtrip(self):
        for state in ("vetting", "awaiting_approval"):
            tid = self.new_task(state=state)
            self.assertEqual(self.board.get_task(tid).state, state)
        tid = self.new_task(state="reviewing")
        t = self.board.get_task(tid)
        self.assertTrue(self.board.update_task(
            tid, expect_state="reviewing", expect_version=t.version, state="blocked",
            then="awaiting_approval", rejected_by="vet", cause="ask", stuck_from="writing",
            review_closure=True, fix_kind="conflict", asked_by="review", last_verify="conflict"))
        t = self.board.get_task(tid)
        self.assertEqual((t.then, t.rejected_by, t.cause, t.stuck_from, t.fix_kind, t.asked_by, t.last_verify),
                         ("awaiting_approval", "vet", "ask", "writing", "conflict", "review", "conflict"))
        self.assertIs(t.review_closure, True)

    def test_states_cover_the_spec(self):
        self.assertEqual(len(B.STATES), 15)
        self.assertEqual(len(set(B.STATES)), 15)


class FactoryTest(unittest.TestCase):
    def test_make_task_covers_every_field(self):
        task = make_task()
        self.assertEqual({f.name for f in dataclasses.fields(task)}, {f.name for f in dataclasses.fields(B.Task)})
        self.assertEqual(task.area, ())
        self.assertEqual(make_task(state="done", id=5).id, 5)
        with self.assertRaises(TypeError):
            make_task(bogus=1)
