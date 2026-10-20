import json
import tempfile
import unittest
from pathlib import Path

from artel import board as B, pauses


class PausesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.board = B.open_board(Path(self._tmp.name) / "board.db")

    def tearDown(self):
        self.board.close()
        self._tmp.cleanup()

    def test_kind_tuples(self):
        # PD-54, PD-60, PD-65 d: the tuples the other tasks take their rules from
        self.assertEqual(pauses.KINDS, ("manual", "base_conflict", "integration_moved", "failures", "limit",
                                        "agent_unavailable", "quota", "disk", "setup_failed"))
        self.assertEqual(pauses.HUMAN_KINDS, ("manual", "base_conflict", "integration_moved", "failures"))
        self.assertEqual(pauses.PROBED_KINDS, ("limit", "agent_unavailable", "quota"))
        self.assertEqual(pauses.TOKEN_FREE_KINDS, ("limit", "quota", "agent_unavailable"))

    def test_add_is_new_once_and_keeps_since(self):
        self.assertTrue(pauses.add(self.board, "limit", "five_hour", now=10.0, until=100.0, setting="s"))
        self.assertFalse(pauses.add(self.board, "limit", "seven_day", now=20.0, until=50.0))
        pause = pauses.active(self.board)["limit"]
        self.assertEqual(pause.since, 10.0)   # catches: since overwritten by the second cause
        self.assertEqual(pause.detail, "five_hour; seven_day")
        self.assertEqual(pause.causes[0], pauses.Cause("five_hour", "s", 100.0))

    def test_same_cause_is_replaced_not_duplicated(self):
        pauses.add(self.board, "quota", "seven_day", now=1.0, until=10.0, setting="quota_stop_seven_day")
        pauses.add(self.board, "quota", "seven_day", now=2.0, until=20.0, setting="quota_stop_seven_day")
        pause = pauses.active(self.board)["quota"]
        self.assertEqual(pause.causes, (pauses.Cause("seven_day", "quota_stop_seven_day", 20.0),))

    def test_second_cause_extends_the_pause(self):
        # catches: updating the detail only (the later end would be lost)
        pauses.add(self.board, "quota", "five_hour", now=1.0, until=100.0)
        pauses.add(self.board, "quota", "seven_day", now=2.0, until=200.0)
        pause = pauses.active(self.board)["quota"]
        self.assertEqual((pause.until, len(pause.causes)), (200.0, 2))
        pauses.add(self.board, "quota", "budget", now=3.0, until=150.0)   # an earlier end does not shorten it
        self.assertEqual(pauses.active(self.board)["quota"].until, 200.0)
        pauses.add(self.board, "quota", "night budget", now=3.0, until=None)
        self.assertIsNone(pauses.active(self.board)["quota"].until)   # an unknown end: probed

    def test_pause_kinds_are_independent(self):
        # catches: a single pause slot
        pauses.add(self.board, "manual", "artel pause", now=1.0)
        pauses.add(self.board, "limit", "429", now=2.0, until=50.0)
        self.assertTrue(pauses.remove(self.board, "limit"))
        self.assertEqual(list(pauses.active(self.board)), ["manual"])
        self.assertFalse(pauses.remove(self.board, "limit"))

    def test_add_keeps_exhausted(self):
        pauses.add(self.board, "limit", "429", now=1.0)
        pauses.set_exhausted(self.board, "limit")
        pauses.add(self.board, "limit", "429 again", now=2.0)
        self.assertTrue(pauses.active(self.board)["limit"].exhausted)
        pauses.set_exhausted(self.board, "manual")   # not active: nothing appears
        self.assertNotIn("manual", pauses.active(self.board))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            pauses.add(self.board, "nap", "x", now=1.0)

    def test_resume_removes_human_kinds(self):
        for kind in pauses.HUMAN_KINDS:
            pauses.add(self.board, kind, kind, now=1.0)
        self.assertEqual(pauses.resume(self.board, force=False), (list(pauses.HUMAN_KINDS), None))
        self.assertEqual(pauses.active(self.board), {})

    def test_resume_refuses_self_ending_kinds_without_force(self):
        # catches the old reason-only check (any pause removed by plain resume)
        pauses.add(self.board, "quota", "seven_day", now=1.0, until=500.0)
        removed, error = pauses.resume(self.board, force=False)
        self.assertEqual(removed, [])
        self.assertEqual(error, "pause quota ends by itself; use resume --force")
        self.assertIn("quota", pauses.active(self.board))

    def test_resume_removes_the_human_kind_and_names_the_one_left(self):
        pauses.add(self.board, "disk", "low", now=1.0)
        pauses.add(self.board, "manual", "artel pause", now=1.0)
        removed, error = pauses.resume(self.board, force=False)
        self.assertEqual(removed, ["manual"])
        self.assertEqual(error, "pause disk ends by itself; use resume --force")
        self.assertEqual(list(pauses.active(self.board)), ["disk"])

    def test_resume_clears_probed_kind_only_when_exhausted(self):
        for kind in pauses.PROBED_KINDS:
            with self.subTest(kind=kind):
                pauses.add(self.board, kind, "probe me", now=1.0)
                removed, error = pauses.resume(self.board, force=False)
                self.assertEqual((removed, error), ([], f"pause {kind} ends by itself; use resume --force"))
                pauses.set_exhausted(self.board, kind)
                self.assertEqual(pauses.resume(self.board, force=False), ([kind], None))
                self.assertEqual(pauses.active(self.board), {})

    def test_resume_force_empties_the_set(self):
        for kind in pauses.KINDS:
            pauses.add(self.board, kind, kind, now=1.0, until=99.0)
        removed, error = pauses.resume(self.board, force=True)
        self.assertEqual((removed, error), (list(pauses.KINDS), None))
        self.assertEqual(pauses.active(self.board), {})
        self.assertFalse(pauses.blocks_agents(self.board))

    def test_resume_failures_resets_both_streaks(self):
        # PD-58, PD-61 amended
        self.board.set_meta("failure_streak", json.dumps({"count": 3, "tasks": [1, 2]}))
        self.board.set_meta("unavailable_streak", json.dumps({"signature": "exit 1: x", "count": 3}))
        pauses.add(self.board, "failures", "3 failures", now=1.0, setting="max_consecutive_failures")
        pauses.resume(self.board, force=False)
        self.assertEqual(json.loads(self.board.get_meta("failure_streak")), {"count": 0, "tasks": []})
        self.assertEqual(json.loads(self.board.get_meta("unavailable_streak")), {"signature": "", "count": 0})

    def test_resume_without_failures_keeps_the_streaks(self):
        self.board.set_meta("failure_streak", json.dumps({"count": 2, "tasks": [1]}))
        pauses.add(self.board, "manual", "artel pause", now=1.0)
        pauses.resume(self.board, force=False)
        self.assertEqual(json.loads(self.board.get_meta("failure_streak"))["count"], 2)

    def test_blocks_verify_only_outside_token_free_kinds(self):
        self.assertFalse(pauses.blocks_verify(self.board))
        for kind in pauses.TOKEN_FREE_KINDS:
            pauses.add(self.board, kind, kind, now=1.0)
        self.assertTrue(pauses.blocks_agents(self.board))
        self.assertFalse(pauses.blocks_verify(self.board))
        pauses.add(self.board, "disk", "low", now=1.0)
        self.assertTrue(pauses.blocks_verify(self.board))

    def test_migrate_legacy_converts_once_and_deletes_the_keys(self):
        self.board.set_meta("pause_reason", "base_conflict")
        self.board.set_meta("pause_detail", "conflict: c.txt")
        self.board.set_meta("pause_until", repr(123.5))
        pauses.migrate_legacy(self.board, now=7.0)
        pause = pauses.active(self.board)["base_conflict"]
        self.assertEqual((pause.since, pause.detail, pause.until), (7.0, "conflict: c.txt", 123.5))
        for key in ("pause_reason", "pause_detail", "pause_until"):
            self.assertIsNone(self.board.get_meta(key))
        pauses.migrate_legacy(self.board, now=8.0)   # nothing left to convert
        self.assertEqual(pauses.active(self.board)["base_conflict"].since, 7.0)

    def test_migrate_legacy_unknown_reason_and_bad_until(self):
        self.board.set_meta("pause_reason", "nap")
        self.board.set_meta("pause_until", "soon")
        pauses.migrate_legacy(self.board, now=1.0)
        pause = pauses.active(self.board)["manual"]   # an unknown reason becomes a manual pause
        self.assertEqual((pause.detail, pause.until), ("nap", None))

    def test_a_kind_this_version_does_not_know_still_counts_and_force_removes_it(self):
        self.board.set_meta("pauses", json.dumps({"future": {"since": 1.0, "causes": [{"detail": "d"}]}}))
        self.assertIn("future", pauses.active(self.board))
        self.assertTrue(pauses.blocks_agents(self.board))
        self.assertEqual(pauses.resume(self.board, force=False),
                         ([], "pause future ends by itself; use resume --force"))
        self.assertEqual(pauses.resume(self.board, force=True), (["future"], None))
        self.assertFalse(pauses.blocks_agents(self.board))

    def test_migrate_legacy_without_keys_is_a_noop(self):
        pauses.migrate_legacy(self.board, now=1.0)
        self.assertEqual(pauses.active(self.board), {})
        self.assertIsNone(self.board.get_meta("pauses"))


if __name__ == "__main__":
    unittest.main()
