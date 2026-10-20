import tempfile
import unittest
from pathlib import Path

from artel import board as B, journal as J


class JournalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.board = B.open_board(Path(self._tmp.name) / "board.db")
        self.tid = self.board.insert_task(title="t", state="reviewing", created_by="human")

    def tearDown(self):
        self.board.close()
        self._tmp.cleanup()

    def finding(self, stage="review", round=1, severity="major", text="bug"):
        return J.add_finding(self.board, task_id=self.tid, stage=stage, round=round, severity=severity,
                             text=text, basis="a.py:3", author=f"agent:{stage}")

    def test_append_and_read_back(self):
        eid = J.append(self.board, "FACT", "hello", author="agent:write", task_id=self.tid, stage="write",
                       round=2, basis="a.py:1")
        e = J.get_entry(self.board, eid)
        self.assertEqual((e.kind, e.text, e.basis, e.author, e.task_id, e.stage, e.round, e.ref),
                         ("FACT", "hello", "a.py:1", "agent:write", self.tid, "write", 2, None))
        self.assertGreater(e.ts, 0)
        self.assertEqual([x.id for x in J.task_entries(self.board, self.tid)], [eid])
        other = J.append(self.board, "FAIL", "x", author="runner")   # not tied to a task
        self.assertIsNone(J.get_entry(self.board, other).task_id)
        self.assertEqual(len(J.task_entries(self.board, self.tid)), 1)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            J.append(self.board, "GOSSIP", "x", author="runner")
        for kind in J.KINDS:
            J.append(self.board, kind, "x", author="runner")

    def test_text_limit_is_bytes_not_chars(self):
        J.append(self.board, "FACT", "я" * 200, author="agent:write", task_id=self.tid)   # 400 bytes
        with self.assertRaises(J.EntryTooLong):
            J.append(self.board, "FACT", "я" * 201, author="agent:write", task_id=self.tid)
        J.append(self.board, "FACT", "a" * 400, author="runner")
        with self.assertRaises(J.EntryTooLong):
            J.append(self.board, "FACT", "a" * 401, author="runner")

    def test_basis_limit(self):
        J.append(self.board, "FACT", "x", author="runner", basis="я" * 100)   # 200 bytes
        with self.assertRaises(J.EntryTooLong) as cm:
            J.append(self.board, "FACT", "x", author="runner", basis="я" * 101)
        self.assertIn("basis", str(cm.exception))
        self.assertIn("202", str(cm.exception))
        self.assertIn("200", str(cm.exception))

    def test_error_message_tells_to_commit_a_file(self):
        with self.assertRaises(J.EntryTooLong) as cm:
            J.append(self.board, "FACT", "a" * 500, author="runner")
        message = str(cm.exception)
        self.assertIn("text", message)
        self.assertIn("500", message)
        self.assertIn("400", message)
        self.assertIn("commit a file and reference it in basis", message)

    def test_too_long_entry_writes_nothing(self):
        with self.assertRaises(J.EntryTooLong):
            J.append(self.board, "FACT", "a" * 500, author="runner", task_id=self.tid)
        self.assertEqual(J.task_entries(self.board, self.tid), [])

    def test_truncate_utf8_never_splits_code_point(self):
        for text in ("я" * 300, "✓" * 300, "😀" * 300, "ab" + "я" * 300):
            cut = J.truncate_utf8(text, 400)
            self.assertLessEqual(J.utf8_len(cut), 400)
            self.assertTrue(cut.endswith("…"))
            cut.encode("utf-8").decode("utf-8")
            self.assertGreater(J.utf8_len(cut), 396)   # no needless loss
        self.assertEqual(J.truncate_utf8("😀" * 4, 7), "😀…")   # 4 + 3 bytes
        self.assertLessEqual(J.utf8_len(J.truncate_utf8("😀" * 4, 2)), 2)

    def test_truncate_utf8_keeps_short_text(self):
        self.assertEqual(J.truncate_utf8("abc", 400), "abc")
        self.assertEqual(J.truncate_utf8("я" * 200, 400), "я" * 200)
        self.assertEqual(J.truncate_utf8("a" * 400, 400), "a" * 400)

    def test_finding_requires_valid_severity(self):
        with self.assertRaises(ValueError):
            self.finding(severity="catastrophic")
        for sev in J.SEVERITIES:
            self.finding(severity=sev)

    def test_severity_only_on_findings(self):
        with self.assertRaises(ValueError):
            J.append(self.board, "FACT", "x", author="runner", severity="major")
        with self.assertRaises(ValueError):
            J.append(self.board, "FACT", "x", author="runner", status="open")
        with self.assertRaises(ValueError):
            J.append(self.board, "FINDING", "x", author="runner", severity="huge")

    def test_closure_is_a_new_entry(self):
        f = self.finding()
        c = J.close_finding(self.board, f, task_id=self.tid, round=2, basis="a.py:3 fixed", author="agent:review")
        self.assertEqual(J.open_findings(self.board, self.tid), [])
        original = J.get_entry(self.board, f)
        self.assertEqual(original.status, "open")   # original never edited
        closing = J.get_entry(self.board, c)
        self.assertEqual((closing.status, closing.ref, closing.text, closing.round, closing.stage),
                         ("closed", f, f"closed #{f}", 2, "review"))
        self.assertNotEqual(c, f)

    def test_close_unknown_or_closed_finding_raises(self):
        f = self.finding()
        fact = J.append(self.board, "FACT", "x", author="runner", task_id=self.tid)
        other_task = self.board.insert_task(title="o", state="reviewing", created_by="human")
        for bad in (9999, fact):
            with self.assertRaises(B.ArtelError):
                J.close_finding(self.board, bad, task_id=self.tid, round=2, basis="", author="agent:review")
        with self.assertRaises(B.ArtelError):   # a finding of another task
            J.close_finding(self.board, f, task_id=other_task, round=2, basis="", author="agent:review")
        closing = J.close_finding(self.board, f, task_id=self.tid, round=2, basis="", author="agent:review")
        with self.assertRaises(B.ArtelError):   # already closed
            J.close_finding(self.board, f, task_id=self.tid, round=3, basis="", author="agent:review")
        with self.assertRaises(B.ArtelError):   # a closing entry is not an open finding
            J.close_finding(self.board, closing, task_id=self.tid, round=3, basis="", author="agent:review")

    def test_open_findings_excludes_attack_stage_by_default(self):
        review = self.finding(stage="review")
        attack = self.finding(stage="attack", text="attack bug")
        self.assertEqual([e.id for e in J.open_findings(self.board, self.tid)], [review])
        self.assertEqual([e.id for e in J.open_findings(self.board, self.tid, stages=("attack",))], [attack])
        both = J.open_findings(self.board, self.tid, stages=("review", "attack"))
        self.assertEqual([e.id for e in both], [review, attack])

    def test_findings_reports_open_and_closed(self):
        a = self.finding(text="a")
        b = self.finding(text="b", severity="minor")
        J.close_finding(self.board, a, task_id=self.tid, round=2, basis="", author="agent:review")
        got = J.findings(self.board, self.tid)
        self.assertEqual([(e.id, is_open) for e, is_open in got], [(a, False), (b, True)])
        self.assertEqual(J.findings(self.board, self.tid, stages=("attack",)), [])

    def test_correct_creates_new_entry_with_ref(self):
        old = J.append(self.board, "FACT", "wrong", author="agent:write", task_id=self.tid, stage="write", round=1)
        new = J.correct(self.board, old, "right", author="runner", basis="a.py:2")
        e = J.get_entry(self.board, new)
        self.assertEqual((e.kind, e.task_id, e.ref, e.text, e.author, e.basis), ("FACT", self.tid, old, "right", "runner", "a.py:2"))
        self.assertEqual(J.get_entry(self.board, old).text, "wrong")
        with self.assertRaises(B.ArtelError):
            J.correct(self.board, 9999, "x", author="runner")

    def test_correcting_a_finding_does_not_count_as_a_finding(self):
        f = self.finding()
        J.correct(self.board, f, "better words", author="runner")
        self.assertEqual([e.id for e in J.open_findings(self.board, self.tid)], [f])

    def test_recent_entries_newest_first_and_filters(self):
        other = self.board.insert_task(title="o", state="open", created_by="human")
        ids = [
            J.append(self.board, "FACT", "1", author="runner", task_id=self.tid),
            J.append(self.board, "FAIL", "2", author="runner", task_id=other),
            J.append(self.board, "FACT", "3", author="runner"),
            J.append(self.board, "DONE", "4", author="runner", task_id=self.tid),
        ]
        self.assertEqual([e.id for e in J.recent_entries(self.board)], ids[::-1])
        self.assertEqual([e.id for e in J.recent_entries(self.board, kinds=["FACT"])], [ids[2], ids[0]])
        self.assertEqual([e.id for e in J.recent_entries(self.board, exclude_task=self.tid)], [ids[2], ids[1]])
        self.assertEqual([e.id for e in J.recent_entries(self.board, limit=2)], ids[:1:-1])
        self.assertEqual(J.recent_entries(self.board, kinds=[]), [])

    def test_no_code_path_updates_or_deletes_entries(self):
        root = Path(__file__).resolve().parents[1] / "artel"
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8").upper()
            self.assertNotIn("UPDATE ENTRIES", text, path.name)
            self.assertNotIn("DELETE FROM ENTRIES", text, path.name)
