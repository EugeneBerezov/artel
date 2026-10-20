import dataclasses
import itertools
import tempfile
import unittest
from pathlib import Path

from artel import board as B, transitions as T
from tests.factories import make_task

LIM = T.Limits(max_attempts=2, review_rounds=2, max_verify_fixes=2, merge="auto", autoapprove="non_risky")


def lim(**kw):
    return dataclasses.replace(LIM, **kw)


def tr(state, event, limits=LIM, **kw):
    """decide() on a task in `state`; task fields via task=…, decide arguments via the rest."""
    task_fields = kw.pop("task", {})
    return T.decide(make_task(state=state, **task_fields), event, limits, **kw)


CLEAR = {"asked_by": None, "cause": None, "answered": True}   # the `clear_ask` update


class RowTests(unittest.TestCase):
    def check(self, t, rid, to, *, effects=(), text="", **updates):
        """Exact: the whole updates dict, the whole effects tuple and the cause text (a stray reset_branch,
        answered=1 or a dropped cause fails)."""
        self.assertEqual((t.rid, t.to_state), (rid, to))
        self.assertEqual(t.updates, updates)
        self.assertEqual(t.effects, effects)
        self.assertEqual(t.cause, text)   # `cause` is also a task column, so the verdict text is `text`

    def test_R01_add_human_goes_open(self):
        t = T.decide(None, "add_human", None)
        self.check(t, "R01", "open")
        self.assertIsNone(t.from_state)

    def test_R02_add_agent_goes_proposed(self):
        self.check(T.decide(None, "add_agent", None), "R02", "proposed")

    def test_R03_pick_proposed_goes_vetting(self):
        self.check(tr("proposed", "pick"), "R03", "vetting")

    def test_R04a_vet_ok_opens_when_autoapprove_non_risky_or_all(self):
        for auto in ("non_risky", "all"):
            with self.subTest(auto=auto):
                self.check(tr("vetting", "vet_ok", lim(autoapprove=auto)), "R04a", "open",
                           text=f"autoapprove={auto}")

    def test_R04b_vet_ok_awaits_approval_when_autoapprove_none(self):
        self.check(tr("vetting", "vet_ok", lim(autoapprove="none")), "R04b", "awaiting_approval",
                   text="autoapprove=none")

    def test_R04c_risky_vet_goes_to_attack_then_approval(self):
        for auto in ("none", "non_risky"):
            with self.subTest(auto=auto):
                self.check(tr("vetting", "vet_risky", lim(autoapprove=auto)), "R04c", "attacking",
                           then="awaiting_approval", risky=True, text=f"autoapprove={auto}")

    def test_R04d_risky_vet_with_autoapprove_all_opens_after_attack(self):
        self.check(tr("vetting", "vet_risky", lim(autoapprove="all")), "R04d", "attacking",
                   then="open", risky=True, text="autoapprove=all")

    def test_R05_vet_inadequate_rejects_by_vet(self):
        self.check(tr("vetting", "vet_inadequate"), "R05", "rejected", rejected_by="vet")

    def test_R06_vet_fail_counts_and_sticks_at_cap(self):
        self.check(tr("vetting", "stage_failed", task={"attempts": 0}), "R06", "vetting", attempts=1,
                   text="attempts 1 < max_attempts 2")
        self.check(tr("vetting", "stage_failed", task={"attempts": 1}), "R06", "stuck", attempts=2,
                   stuck_from="vetting", text="attempts 2 >= max_attempts 2")

    def test_R07a_attack_clear_awaits_approval_when_vet_asked_for_it(self):
        self.check(tr("attacking", "attack_clear", task={"then": "awaiting_approval"}), "R07a",
                   "awaiting_approval", then=None, effects=("append_findings",))

    def test_R07b_attack_clear_opens_when_vet_said_open(self):
        self.check(tr("attacking", "attack_clear", task={"then": "open"}), "R07b", "open", then=None,
                   effects=("append_findings",))

    def test_R08_approve_from_proposed_awaiting_and_vet_rejected(self):
        for state, extra in (("proposed", {}), ("awaiting_approval", {}), ("rejected", {"rejected_by": "vet"})):
            with self.subTest(state=state):
                self.check(tr(state, "approve", task=extra), "R08", "open", rejected_by=None)
        with self.assertRaises(T.TransitionError):
            tr("rejected", "approve", task={"rejected_by": "human"})

    def test_R09_reject_from_every_live_state(self):
        spec_sources = ("proposed", "vetting", "awaiting_approval", "open", "attacking", "writing", "reviewing",
                        "fixing", "verifying", "verified", "blocked", "stuck", "awaiting_accept")
        self.assertEqual(set(T.LIVE), set(spec_sources))   # catches dropping stuck/awaiting_accept from LIVE
        for state in spec_sources:
            with self.subTest(state=state):
                self.check(tr(state, "reject"), "R09", "rejected", rejected_by="human",
                           effects=("terminate_running", "delete_branch"))
        with self.assertRaises(T.TransitionError):
            tr("done", "reject")

    def test_R10a_pick_open_with_attack(self):
        self.check(tr("open", "pick", task={"attack": True}), "R10a", "attacking")

    def test_R10b_pick_open_without_attack(self):
        self.check(tr("open", "pick", task={"attack": False}), "R10b", "writing")

    def test_R11_attack_clear_without_then_goes_writing(self):
        self.check(tr("attacking", "attack_clear", task={"then": None}), "R11", "writing",
                   effects=("append_findings",))

    def test_R12_attack_blocker_blocks_with_a_question(self):
        self.check(tr("attacking", "attack_blocker"), "R12", "blocked", asked_by="attack", cause="ask")

    def test_R13_attack_and_review_fail_count_then_stick(self):
        for state in ("attacking", "reviewing"):
            with self.subTest(state=state):
                self.check(tr(state, "stage_failed", task={"attempts": 0}), "R13", state, attempts=1,
                           text="attempts 1 < max_attempts 2")
                self.check(tr(state, "stage_failed", task={"attempts": 1}), "R13", "stuck", attempts=2,
                           stuck_from=state, text="attempts 2 >= max_attempts 2")

    def test_R14a_writer_ready_goes_reviewing_round_one(self):
        self.check(tr("writing", "ready", task={"round": 0}), "R14a", "reviewing", round=1)

    def test_R14b_review_fix_ready_increments_round(self):
        self.check(tr("fixing", "ready", task={"fix_kind": "review", "round": 1}), "R14b", "reviewing",
                   round=2, fix_kind=None)

    def test_R15_verify_fix_ready_goes_verifying_without_round(self):
        self.check(tr("fixing", "ready", task={"fix_kind": "verify", "round": 2}), "R15", "verifying",
                   fix_kind=None)

    def test_R16_conflict_fix_ready_reviews_with_closure_template(self):
        self.check(tr("fixing", "ready", task={"fix_kind": "conflict", "round": 2}), "R16", "reviewing",
                   fix_kind=None, review_closure=True)

    def test_R17_writer_fail_resets_and_sticks_as_writing(self):
        fields = {"fix_kind": "verify", "round": 2, "verify_fixes": 1, "attempts": 0, "fix_base": "abc",
                  "accepted": True, "review_closure": True}
        reset = dict(round=0, verify_fixes=0, fix_kind=None, fix_base=None, accepted=False, review_closure=False)
        self.check(tr("fixing", "stage_failed", task=fields), "R17", "open", attempts=1, **reset,
                   effects=("reset_branch",), text="attempts 1 < max_attempts 2")
        for state in ("fixing", "writing"):
            self.check(tr(state, "stage_failed", task=dict(fields, attempts=1)), "R17", "stuck", attempts=2,
                       stuck_from="writing", **reset, effects=("reset_branch",),
                       text="attempts 2 >= max_attempts 2")

    def test_R18_ask_from_each_stage_state(self):
        for state, stage in (("vetting", "vet"), ("attacking", "attack"), ("writing", "write"),
                             ("reviewing", "review"), ("fixing", "fix")):
            with self.subTest(state=state):
                self.check(tr(state, "ask", stage=stage), "R18", "blocked", asked_by=stage, cause="ask")
                with self.assertRaises(T.TransitionError):
                    tr(state, "ask", stage="verify")
        with self.assertRaises(T.TransitionError):
            tr("writing", "ask")

    def test_R19_setup_failed_changes_nothing_but_pauses(self):
        for state in ("vetting", "attacking", "writing", "reviewing", "fixing", "verifying"):
            with self.subTest(state=state):
                self.check(tr(state, "setup_failed"), "R19", state, effects=("pause_setup_failed",))

    def test_R20_internal_error_counts_and_sticks(self):
        self.check(tr("verifying", "internal_error", task={"attempts": 0}), "R20", "verifying", attempts=1,
                   effects=("notify_internal_error",), text="attempts 1 < max_attempts 2")
        for state in T.JOB_STATES:   # every source state sticks from itself (R17's "writing" is not borrowed)
            with self.subTest(state=state):
                self.check(tr(state, "internal_error", task={"attempts": 1}), "R20", "stuck", attempts=2,
                           stuck_from=state, effects=("notify_internal_error",),
                           text="attempts 2 >= max_attempts 2")
        self.assertEqual(set(T.JOB_STATES), {"vetting", "attacking", "writing", "reviewing", "fixing", "verifying"})

    def test_R21_review_clear_goes_verifying(self):
        self.check(tr("reviewing", "review_clear", task={"review_closure": True}), "R21", "verifying",
                   review_closure=False)

    def test_R22a_blocking_review_below_cap_goes_fixing(self):
        self.check(tr("reviewing", "review_blocking", task={"round": 1}, tip="deadbeef"), "R22a", "fixing",
                   fix_kind="review", review_closure=False, fix_base="deadbeef",
                   text="round 1 < review_rounds 2")
        with self.assertRaises(T.TransitionError):
            tr("reviewing", "review_blocking", task={"round": 1})   # no tip
        self.check(tr("reviewing", "review_blocking", lim(review_rounds=3), task={"round": 2}, tip="x"),
                   "R22a", "fixing", fix_kind="review", review_closure=False, fix_base="x",
                   text="round 2 < review_rounds 3")
        effective = T.Limits.for_task(make_task(extra_review_rounds=1), max_attempts=2, review_rounds=2,
                                      max_verify_fixes=2, merge="auto", autoapprove="non_risky")
        self.check(T.decide(make_task(state="reviewing", round=2, extra_review_rounds=1), "review_blocking",
                            effective, tip="x"), "R22a", "fixing", fix_kind="review", review_closure=False,
                   fix_base="x", text="round 2 < review_rounds 3")

    def test_R22b_blocking_review_at_cap_asks_the_human(self):
        self.check(tr("reviewing", "review_blocking", task={"round": 2}), "R22b", "blocked", asked_by="review",
                   cause="cap", review_closure=False, effects=("ask_open_findings",),
                   text="round 2 >= review_rounds 2")

    def test_R23_answer_after_attack_keeps_then(self):
        self.check(tr("blocked", "answer", task={"asked_by": "attack", "cause": "ask", "then": "open"}),
                   "R23", "attacking", effects=("append_answer",), **CLEAR)

    def test_R24_answer_resumes_write_or_fix(self):
        self.check(tr("blocked", "answer", task={"asked_by": "write", "cause": "ask"}), "R24", "writing",
                   effects=("append_answer",), **CLEAR)
        self.check(tr("blocked", "answer", task={"asked_by": "fix", "cause": "ask", "fix_kind": "verify"}),
                   "R24", "fixing", effects=("append_answer",), **CLEAR)   # fix_kind is kept

    def test_R25_answer_to_reviewer_question_returns_to_review(self):
        self.check(tr("blocked", "answer", task={"asked_by": "review", "cause": "ask", "round": 1}), "R25",
                   "reviewing", effects=("append_answer",), **CLEAR)

    def test_R26_answer_at_review_cap_grants_an_extra_round(self):
        self.check(tr("blocked", "answer", task={"asked_by": "review", "cause": "cap", "extra_review_rounds": 1},
                      tip="t1"), "R26", "fixing", fix_kind="review", extra_review_rounds=2, fix_base="t1",
                   effects=("append_answer",), **CLEAR)

    def test_R27_answer_after_vet_question_returns_to_vetting(self):
        self.check(tr("blocked", "answer", task={"asked_by": "vet", "cause": "ask"}), "R27", "vetting",
                   effects=("append_answer",), **CLEAR)

    def test_R28_answer_after_verify_cap_grants_an_extra_fix(self):
        self.check(tr("blocked", "answer", task={"asked_by": "verify", "cause": "cap", "extra_verify_fixes": 0},
                      tip="t2"), "R28", "fixing", fix_kind="verify", extra_verify_fixes=1, fix_base="t2",
                   effects=("append_answer",), **CLEAR)

    def test_R29a_rework_blocked_with_branch(self):
        self.check(tr("blocked", "rework", task={"asked_by": "review", "cause": "cap"}, has_branch=True, tip="t3"),
                   "R29a", "fixing", fix_kind="review", extra_review_rounds=1, fix_base="t3",
                   effects=("append_answer",), **CLEAR)
        with self.assertRaises(T.TransitionError):
            tr("blocked", "rework", task={"asked_by": "review", "cause": "cap"}, tip="t3")   # has_branch missing

    def test_R29b_rework_stuck_with_branch_resets_attempts(self):
        self.check(tr("stuck", "rework", task={"attempts": 2, "stuck_from": "reviewing", "accepted": True},
                      has_branch=True, tip="t4"), "R29b", "fixing", fix_kind="review", extra_review_rounds=1,
                   attempts=0, stuck_from=None, answered=True, fix_base="t4", accepted=False,
                   effects=("append_answer",))

    def test_R30_rework_stuck_without_branch_returns_to_the_stuck_stage(self):
        for stage in ("vetting", "attacking", "writing"):
            with self.subTest(stage=stage):
                self.check(tr("stuck", "rework", task={"attempts": 2, "stuck_from": stage}, has_branch=False),
                           "R30", stage, attempts=0, stuck_from=None, answered=True,
                           effects=("append_answer",))
        with self.assertRaises(T.TransitionError):
            tr("stuck", "rework", task={"stuck_from": "writing"})

    def test_R31a_verify_green_verifies_when_merge_auto_or_accepted(self):
        self.check(tr("verifying", "verify_green"), "R31a", "verified", text="merge=auto")
        self.check(tr("verifying", "verify_green", lim(merge="ask"), task={"accepted": True}), "R31a",
                   "verified", text="merge=ask, accepted")
        self.check(tr("verifying", "verify_green", task={"merge": None, "accepted": True}), "R31a", "verified",
                   text="merge=auto")   # accepted under merge=auto is not "ask, accepted"

    def test_R31b_verify_green_awaits_accept_when_merge_ask(self):
        self.check(tr("verifying", "verify_green", lim(merge="ask")), "R31b", "awaiting_accept",
                   text="merge=ask")
        eff = T.Limits.for_task(make_task(merge="ask"), max_attempts=2, review_rounds=2, max_verify_fixes=2,
                                merge="auto", autoapprove="non_risky")
        self.check(tr("verifying", "verify_green", eff), "R31b", "awaiting_accept", text="merge=ask")

    def test_R32_conflict_below_cap_goes_to_conflict_fix(self):
        self.check(tr("verifying", "verify_conflict", task={"verify_fixes": 1, "accepted": True}), "R32",
                   "fixing", fix_kind="conflict", verify_fixes=2, accepted=False,
                   text="verify_fixes 1 < max_verify_fixes 2")

    def test_R33_red_below_cap_goes_to_verify_fix(self):
        self.check(tr("verifying", "verify_red", task={"verify_fixes": 0, "accepted": True}, tip="t5"), "R33",
                   "fixing", fix_kind="verify", verify_fixes=1, accepted=False, fix_base="t5",
                   text="verify_fixes 0 < max_verify_fixes 2")

    def test_R34a_red_at_cap_blocks_with_a_question(self):
        t = tr("verifying", "verify_red", task={"verify_fixes": 2, "accepted": True})
        self.assertEqual((t.rid, t.to_state, t.updates, t.effects, t.cause),
                         ("R34a", "blocked", {"asked_by": "verify", "cause": "cap", "accepted": False}, (),
                          "verify_fixes 2 >= max_verify_fixes 2"))

    def test_R34b_conflict_at_cap_blocks_with_a_question(self):
        t = tr("verifying", "verify_conflict", task={"verify_fixes": 2, "accepted": True})
        self.assertEqual((t.rid, t.to_state, t.updates, t.effects, t.cause),
                         ("R34b", "blocked", {"asked_by": "verify", "cause": "cap", "accepted": False}, (),
                          "verify_fixes 2 >= max_verify_fixes 2"))

    def test_R35_accept_sets_the_flag(self):
        self.check(tr("awaiting_accept", "accept"), "R35", "verified", accepted=True)

    def test_R36_fast_forward_done_deletes_branch(self):
        self.check(tr("verified", "ff_ok"), "R36", "done", effects=("delete_branch",))

    def test_R37_ff_impossible_reverifies_without_updates(self):
        self.check(tr("verified", "ff_impossible"), "R37", "verifying")


class MetaTests(unittest.TestCase):
    def test_every_row_has_a_test(self):
        names = {n for n in dir(RowTests) if n.startswith("test_R")}
        for row in T.TABLE:
            self.assertTrue(any(n.startswith(f"test_{row.rid}_") for n in names), row.rid)

    def test_table_rows_are_unambiguous(self):
        samples = []
        for fix_kind, then, asked_by, cause, accepted, rejected_by in itertools.product(
                (None, "review", "verify", "conflict"), (None, "open", "awaiting_approval"),
                (None, "vet", "attack", "write", "review", "fix", "verify"), (None, "ask", "cap"),
                (False, True), (None, "vet", "human")):
            samples.append(dict(fix_kind=fix_kind, then=then, asked_by=asked_by, cause=cause,
                                accepted=accepted, rejected_by=rejected_by))
        counters = [dict(round=r, verify_fixes=v, attempts=a, attack=k) for r, v, a, k in
                    itertools.product((0, 1, 2, 3), (0, 1, 2, 3), (0, 1, 2), (False, True))]
        limits = [lim(autoapprove=a, merge=m) for a in ("none", "non_risky", "all") for m in ("auto", "ask")]
        checked = 0
        for state in B.STATES:
            for event in T.EVENTS:
                for i, fields in enumerate(samples[::5]):
                    extra = counters[i % len(counters)]
                    for has_branch, limit in ((True, limits[i % 6]), (False, limits[(i + 1) % 6])):
                        task = make_task(state=state, stuck_from="writing", **fields, **extra)
                        rows = T.matching_rows(task, event, limit, stage="write", has_branch=has_branch, tip="t")
                        self.assertLessEqual(len(rows), 1, (state, event, [r.rid for r in rows]))
                        checked += 1
        self.assertGreater(checked, 10000)

    def test_every_state_and_event_is_known(self):
        for row in T.TABLE:
            self.assertIn(row.event, T.EVENTS, row.rid)
            self.assertTrue(set(row.sources) <= set(B.STATES), row.rid)
            self.assertTrue(row.to in B.STATES or row.to in ("=", "@asked", "@stuck_from"), row.rid)
            self.assertTrue(set(row.effects) <= set(T.EFFECTS), row.rid)
        self.assertEqual(len({r.rid for r in T.TABLE}), len(T.TABLE))

    def test_unlisted_transition_is_error_and_board_unchanged(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            b = B.open_board(Path(d) / "b.db")
            self.addCleanup(b.close)
            tid = b.insert_task(title="t", state="done", created_by="human")
            with self.assertRaises(T.TransitionError):
                T.apply(b, tid, "answer", LIM)
            self.assertEqual(b.get_task(tid).state, "done")
            self.assertEqual(b.get_task(tid).version, 0)

    def test_unknown_event_is_error(self):
        with self.assertRaises(T.TransitionError):
            tr("open", "frobnicate")

    def test_requeue_is_not_a_transition(self):
        self.assertNotIn("requeue", T.EVENTS)
        self.assertFalse([r for r in T.TABLE if "requeue" in r.event])

    def test_apply_updates_the_board_and_raises_stale_on_lost_cas(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            b = B.open_board(Path(d) / "b.db")
            self.addCleanup(b.close)
            tid = T.create(b, "add_human", title="t")
            t = T.apply(b, tid, "pick", LIM)
            self.assertEqual((t.rid, b.get_task(tid).state), ("R10b", "writing"))
            self.assertEqual(b.get_task(tid).version, 1)
            task = b.get_task(tid)
            orig = b.get_task
            b.get_task = lambda _id: task   # a stale snapshot: the row moved on after it was read
            self.assertTrue(b.update_task(tid, expect_state="writing", expect_version=1, round=1))
            with self.assertRaises(T.StaleTask):
                T.apply(b, tid, "ready", LIM)
            b.get_task = orig
            self.assertEqual(b.get_task(tid).state, "writing")
            self.assertEqual(b.get_task(tid).round, 1)

    def test_create_uses_R01_R02(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            b = B.open_board(Path(d) / "b.db")
            self.addCleanup(b.close)
            human = T.create(b, "add_human", title="h", area=["src"])
            agent = T.create(b, "add_agent", title="a", created_by="agent:1")
            self.assertEqual((b.get_task(human).state, b.get_task(human).created_by), ("open", "human"))
            self.assertEqual((b.get_task(agent).state, b.get_task(agent).created_by), ("proposed", "agent:1"))
            with self.assertRaises(T.TransitionError):
                T.create(b, "pick", title="x")

    def test_limits_for_task_effective_values(self):
        task = make_task(review_rounds=4, extra_review_rounds=1, extra_verify_fixes=2, merge="ask")
        got = T.Limits.for_task(task, max_attempts=2, review_rounds=2, max_verify_fixes=2, merge="auto",
                                autoapprove="all")
        self.assertEqual(got, T.Limits(max_attempts=2, review_rounds=5, max_verify_fixes=4, merge="ask",
                                       autoapprove="all"))
        plain = T.Limits.for_task(make_task(), max_attempts=3, review_rounds=2, max_verify_fixes=2,
                                  merge="auto", autoapprove="none")
        self.assertEqual((plain.review_rounds, plain.max_verify_fixes, plain.merge), (2, 2, "auto"))

    def test_limits_review_rounds_zero_is_zero_not_the_default(self):
        """Mutation caught: `task.review_rounds or default` (--review 0 would fall back to the default)."""
        zero = T.Limits.for_task(make_task(review_rounds=0), max_attempts=2, review_rounds=2, max_verify_fixes=2,
                                 merge="auto", autoapprove="none")
        self.assertEqual(zero.review_rounds, 0)
        extra = T.Limits.for_task(make_task(review_rounds=0, extra_review_rounds=2), max_attempts=2,
                                  review_rounds=2, max_verify_fixes=2, merge="auto", autoapprove="none")
        self.assertEqual(extra.review_rounds, 2)

    def test_create_agent_task_without_created_by_is_a_transition_error(self):
        """Mutation caught: a raw TypeError from insert_task for add_agent without created_by."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            b = B.open_board(Path(d) / "b.db")
            self.addCleanup(b.close)
            with self.assertRaises(T.TransitionError):
                T.create(b, "add_agent", title="x")
            self.assertEqual(b.list_tasks(), [])

    def test_limits_from_cfg(self):
        cfg = {"max_attempts": 5, "review_rounds": 3, "max_verify_fixes": 1, "merge": "ask",
               "autoapprove": "none", "unrelated": 1}
        got = T.Limits.from_cfg(make_task(extra_verify_fixes=1), cfg)
        self.assertEqual(got, T.Limits(5, 3, 2, "ask", "none"))

    def test_rows_entering_fixing_set_fix_base(self):
        for row in T.TABLE:
            if row.to == "fixing":
                if row.rid == "R32":
                    self.assertNotIn("fix_base=tip", row.sets)   # a conflict fix is judged by merge_committed
                else:
                    self.assertIn("fix_base=tip", row.sets, row.rid)

    def test_accept_loop_ends(self):
        ask = lim(merge="ask")
        t = tr("verifying", "verify_green", ask)
        self.assertEqual(t.to_state, "awaiting_accept")
        t = tr("awaiting_accept", "accept", ask)
        self.assertEqual(t.to_state, "verified")
        t = tr("verified", "ff_impossible", ask, task={"accepted": True})
        self.assertEqual(t.to_state, "verifying")
        t = tr("verifying", "verify_green", ask, task={"accepted": True})
        self.assertEqual((t.rid, t.to_state), ("R31a", "verified"))

    def test_every_answering_row_clears_the_ask_and_records_the_answer(self):
        cases = [("blocked", {"asked_by": "vet", "cause": "ask"}, "answer", {}),
                 ("blocked", {"asked_by": "attack", "cause": "ask"}, "answer", {}),
                 ("blocked", {"asked_by": "write", "cause": "ask"}, "answer", {}),
                 ("blocked", {"asked_by": "fix", "cause": "ask"}, "answer", {}),
                 ("blocked", {"asked_by": "review", "cause": "ask"}, "answer", {}),
                 ("blocked", {"asked_by": "review", "cause": "cap"}, "answer", {"tip": "t"}),
                 ("blocked", {"asked_by": "verify", "cause": "cap"}, "answer", {"tip": "t"}),
                 ("blocked", {"asked_by": "review", "cause": "cap"}, "rework", {"has_branch": True, "tip": "t"}),
                 ("stuck", {"stuck_from": "writing"}, "rework", {"has_branch": True, "tip": "t"}),
                 ("stuck", {"stuck_from": "writing"}, "rework", {"has_branch": False})]
        for state, fields, event, kw in cases:
            with self.subTest(fields=fields, event=event):
                t = tr(state, event, task=fields, **kw)
                self.assertEqual(t.effects, ("append_answer",))
                self.assertIs(t.updates["answered"], True)
                if state == "blocked":
                    self.assertEqual((t.updates["asked_by"], t.updates["cause"]), (None, None))

    def test_counted_failures_below_the_cap_carry_a_cause(self):
        t = tr("vetting", "stage_failed", task={"attempts": 0})
        self.assertEqual(t.cause, "attempts 1 < max_attempts 2")

    def test_closure_review_does_not_leak_through_a_failed_rewrite(self):
        """Mutation caught: R17 not clearing review_closure (a fresh review would run as a closure, PD-4)."""
        task = make_task(state="fixing", fix_kind="conflict", round=2)
        t = T.decide(task, "ready", LIM)                                     # R16: closure review
        self.assertEqual((t.rid, t.updates["review_closure"]), ("R16", True))
        task = dataclasses.replace(task, state="reviewing", fix_kind=None, review_closure=True)
        t = T.decide(task, "ask", LIM, stage="review")                       # reviewer asks
        task = dataclasses.replace(task, state="blocked", asked_by="review", cause="ask")
        t = T.decide(task, "rework", LIM, has_branch=True, tip="t")          # R29a -> fixing
        self.assertEqual(t.rid, "R29a")
        task = dataclasses.replace(task, state="fixing", fix_kind="review", asked_by=None, cause=None)
        t = T.decide(task, "stage_failed", LIM)                              # R17: branch reset
        self.assertEqual((t.rid, t.to_state), ("R17", "open"))
        task = dataclasses.replace(task, state="writing", **{k: v for k, v in t.updates.items()
                                                              if k != "stuck_from"})
        t = T.decide(task, "ready", LIM)                                     # R14a: the next review
        self.assertFalse(task.review_closure)

    def test_decide_is_pure(self):
        task = make_task(state="verifying", verify_fixes=0)
        before = dataclasses.asdict(task)
        T.decide(task, "verify_red", LIM, tip="t")
        self.assertEqual(dataclasses.asdict(task), before)

    def test_guard_needing_limits_without_limits_is_error(self):
        with self.assertRaises(T.TransitionError):
            T.decide(make_task(state="reviewing"), "review_blocking", None, tip="t")
        with self.assertRaises(T.TransitionError):
            T.decide(make_task(state="vetting"), "vet_ok", None)

    def test_module_imports_no_git_or_process_code(self):
        source = (Path(T.__file__)).read_text(encoding="utf-8")
        for banned in ("subprocess", "gitutil", "journal", "platform"):
            self.assertNotIn("import " + banned, source)
            self.assertNotIn("from artel." + banned, source)


if __name__ == "__main__":
    unittest.main()
