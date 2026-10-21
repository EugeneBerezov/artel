import dataclasses
import itertools
import re
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
HUMAN = dict(CLEAR, stage_runs=0)   # `clear_ask` and the stage_runs reset every answering row has


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
                self.check(tr("vetting", "vet_ok", lim(autoapprove=auto)), "R04a", "open", attempts=0,
                           text=f"autoapprove={auto}")

    def test_R04b_vet_ok_awaits_approval_when_autoapprove_none(self):
        self.check(tr("vetting", "vet_ok", lim(autoapprove="none")), "R04b", "awaiting_approval", attempts=0,
                   text="autoapprove=none")

    def test_R04c_risky_vet_goes_to_attack_then_approval(self):
        for auto in ("none", "non_risky"):
            with self.subTest(auto=auto):
                self.check(tr("vetting", "vet_risky", lim(autoapprove=auto)), "R04c", "attacking", attempts=0,
                           then="awaiting_approval", risky=True, text=f"autoapprove={auto}")

    def test_R04d_risky_vet_with_autoapprove_all_opens_after_attack(self):
        self.check(tr("vetting", "vet_risky", lim(autoapprove="all")), "R04d", "attacking", attempts=0,
                   then="open", risky=True, text="autoapprove=all")

    def test_R05_vet_inadequate_rejects_by_vet(self):
        self.check(tr("vetting", "vet_inadequate"), "R05", "rejected", attempts=0, rejected_by="vet")

    def test_R06_vet_fail_counts_and_sticks_at_cap(self):
        self.check(tr("vetting", "stage_failed", task={"attempts": 0}), "R06", "vetting", attempts=1,
                   text="attempts 1 < max_attempts 2")
        self.check(tr("vetting", "stage_failed", task={"attempts": 1}), "R06", "stuck", attempts=2,
                   stuck_from="vetting", text="attempts 2 >= max_attempts 2")

    def test_R07a_attack_clear_awaits_approval_when_vet_asked_for_it(self):
        self.check(tr("attacking", "attack_clear", task={"then": "awaiting_approval"}), "R07a",
                   "awaiting_approval", attempts=0, then=None, effects=("append_findings",))

    def test_R07b_attack_clear_opens_when_vet_said_open(self):
        self.check(tr("attacking", "attack_clear", task={"then": "open"}), "R07b", "open", attempts=0, then=None,
                   effects=("append_findings",))

    def test_R08_approve_from_proposed_awaiting_and_vet_rejected(self):
        for state, extra in (("proposed", {}), ("awaiting_approval", {}), ("rejected", {"rejected_by": "vet"})):
            with self.subTest(state=state):
                self.check(tr(state, "approve", task=extra), "R08", "open", rejected_by=None, stage_runs=0)
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
        self.check(tr("attacking", "attack_clear", task={"then": None}), "R11", "writing", attempts=0,
                   effects=("append_findings",))

    def test_R12_attack_blocker_blocks_with_a_question(self):
        self.check(tr("attacking", "attack_blocker"), "R12", "blocked", attempts=0, asked_by="attack", cause="ask")

    def test_R13_attack_and_review_fail_count_then_stick(self):
        for state in ("attacking", "reviewing"):
            with self.subTest(state=state):
                self.check(tr(state, "stage_failed", task={"attempts": 0}), "R13", state, attempts=1,
                           text="attempts 1 < max_attempts 2")
                self.check(tr(state, "stage_failed", task={"attempts": 1}), "R13", "stuck", attempts=2,
                           stuck_from=state, text="attempts 2 >= max_attempts 2")

    def test_R14a_writer_ready_goes_reviewing_round_one(self):
        self.check(tr("writing", "ready", task={"round": 0}), "R14a", "reviewing", attempts=0, round=1)

    def test_R14b_review_fix_ready_increments_round(self):
        self.check(tr("fixing", "ready", task={"fix_kind": "review", "round": 1}), "R14b", "reviewing", attempts=0,
                   round=2, fix_kind=None, review_closure=0)

    def test_R15_verify_fix_ready_goes_reviewing_closure_2(self):
        """Mutation caught: R15 left at `verifying` (a verify fix merged unreviewed), a round added, fix_base dropped."""
        t = tr("fixing", "ready", task={"fix_kind": "verify", "round": 2, "attempts": 1})
        self.check(t, "R15", "reviewing", fix_kind=None, review_closure=2, attempts=0)
        self.assertNotIn("round", t.updates)
        self.assertNotIn("fix_base", t.updates)

    def test_R16_conflict_fix_ready_reviews_with_closure_template(self):
        self.check(tr("fixing", "ready", task={"fix_kind": "conflict", "round": 2}), "R16", "reviewing", attempts=0,
                   fix_kind=None, review_closure=1)

    def test_R17_writer_fail_resets_and_sticks_as_writing(self):
        fields = {"fix_kind": "verify", "round": 2, "verify_fixes": 1, "attempts": 0, "fix_base": "abc",
                  "accepted": True, "review_closure": 1}
        reset = dict(round=0, verify_fixes=0, fix_kind=None, fix_base=None, accepted=False, review_closure=0)
        self.check(tr("writing", "stage_failed", task=fields), "R17", "open", attempts=1, **reset,
                   effects=("reset_branch",), text="attempts 1 < max_attempts 2")
        self.check(tr("writing", "stage_failed", task=dict(fields, attempts=1)), "R17", "stuck", attempts=2,
                   stuck_from="writing", **reset, effects=("reset_branch",), text="attempts 2 >= max_attempts 2")

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
        self.check(tr("reviewing", "review_clear", task={"review_closure": 1}), "R21", "verifying", attempts=0,
                   review_closure=0)

    def test_R22a_blocking_review_below_cap_goes_fixing(self):
        self.check(tr("reviewing", "review_blocking", task={"round": 1}, tip="deadbeef"), "R22a", "fixing", attempts=0,
                   fix_kind="review", review_closure=0, fix_base="deadbeef",
                   text="round 1 < review_rounds 2")
        with self.assertRaises(T.TransitionError):
            tr("reviewing", "review_blocking", task={"round": 1})   # no tip
        self.check(tr("reviewing", "review_blocking", lim(review_rounds=3), task={"round": 2}, tip="x"),
                   "R22a", "fixing", attempts=0, fix_kind="review", review_closure=0, fix_base="x",
                   text="round 2 < review_rounds 3")
        effective = T.Limits.for_task(make_task(extra_review_rounds=1), max_attempts=2, review_rounds=2,
                                      max_verify_fixes=2, merge="auto", autoapprove="non_risky")
        self.check(T.decide(make_task(state="reviewing", round=2, extra_review_rounds=1), "review_blocking",
                            effective, tip="x"), "R22a", "fixing", attempts=0, fix_kind="review", review_closure=0,
                   fix_base="x", text="round 2 < review_rounds 3")

    def test_R22b_blocking_review_at_cap_asks_the_human(self):
        self.check(tr("reviewing", "review_blocking", task={"round": 2}), "R22b", "blocked", attempts=0, asked_by="review",
                   cause="cap", review_closure=0, effects=("ask_open_findings",),
                   text="round 2 >= review_rounds 2")

    def test_R23_answer_after_attack_keeps_then(self):
        self.check(tr("blocked", "answer", task={"asked_by": "attack", "cause": "ask", "then": "open"}),
                   "R23", "attacking", effects=("append_answer",), **HUMAN)

    def test_R24_answer_resumes_write_or_fix(self):
        self.check(tr("blocked", "answer", task={"asked_by": "write", "cause": "ask"}), "R24", "writing",
                   effects=("append_answer",), **HUMAN)
        self.check(tr("blocked", "answer", task={"asked_by": "fix", "cause": "ask", "fix_kind": "verify"}),
                   "R24", "fixing", effects=("append_answer",), **HUMAN)   # fix_kind is kept

    def test_R25_answer_to_reviewer_question_returns_to_review(self):
        self.check(tr("blocked", "answer", task={"asked_by": "review", "cause": "ask", "round": 1}), "R25",
                   "reviewing", effects=("append_answer",), **HUMAN)

    def test_R26_answer_at_review_cap_grants_an_extra_round(self):
        self.check(tr("blocked", "answer", task={"asked_by": "review", "cause": "cap", "extra_review_rounds": 1},
                      tip="t1"), "R26", "fixing", fix_kind="review", extra_review_rounds=2, fix_base="t1",
                   effects=("append_answer",), **HUMAN)

    def test_R27_answer_after_vet_question_returns_to_vetting(self):
        self.check(tr("blocked", "answer", task={"asked_by": "vet", "cause": "ask"}), "R27", "vetting",
                   effects=("append_answer",), **HUMAN)

    def test_R28_answer_after_verify_cap_grants_an_extra_fix(self):
        self.check(tr("blocked", "answer", task={"asked_by": "verify", "cause": "cap", "extra_verify_fixes": 0},
                      tip="t2"), "R28", "fixing", fix_kind="verify", extra_verify_fixes=1, fix_base="t2",
                   effects=("append_answer",), **HUMAN)

    def test_R29a_rework_blocked_with_branch(self):
        self.check(tr("blocked", "rework", task={"asked_by": "review", "cause": "cap"}, has_branch=True, tip="t3"),
                   "R29a", "fixing", fix_kind="review", extra_review_rounds=1, fix_base="t3",
                   effects=("append_answer",), **HUMAN)
        with self.assertRaises(T.TransitionError):
            tr("blocked", "rework", task={"asked_by": "review", "cause": "cap"}, tip="t3")   # has_branch missing

    def test_R29b_rework_stuck_with_branch_resets_attempts(self):
        self.check(tr("stuck", "rework", task={"attempts": 2, "stuck_from": "reviewing", "accepted": True},
                      has_branch=True, tip="t4"), "R29b", "fixing", fix_kind="review", extra_review_rounds=1,
                   attempts=0, stuck_from=None, answered=True, fix_base="t4", accepted=False, resets=0,
                   stage_runs=0, effects=("append_answer",))

    def test_R30_rework_stuck_without_branch_returns_to_the_stuck_stage(self):
        for stage in ("vetting", "attacking", "writing"):
            with self.subTest(stage=stage):
                self.check(tr("stuck", "rework", task={"attempts": 2, "stuck_from": stage}, has_branch=False),
                           "R30", stage, attempts=0, resets=0, stuck_from=None, answered=True,
                           stage_runs=0, effects=("append_answer",))
        with self.assertRaises(T.TransitionError):
            tr("stuck", "rework", task={"stuck_from": "writing"})

    def test_R31a_verify_green_verifies_when_merge_auto_or_accepted(self):
        self.check(tr("verifying", "verify_green"), "R31a", "verified", attempts=0, text="merge=auto")
        self.check(tr("verifying", "verify_green", lim(merge="ask"), task={"accepted": True}), "R31a",
                   "verified", attempts=0, text="merge=ask, accepted")
        self.check(tr("verifying", "verify_green", task={"merge": None, "accepted": True}), "R31a", "verified", attempts=0,
                   text="merge=auto")   # accepted under merge=auto is not "ask, accepted"

    def test_R31b_verify_green_awaits_accept_when_merge_ask(self):
        self.check(tr("verifying", "verify_green", lim(merge="ask")), "R31b", "awaiting_accept", attempts=0,
                   text="merge=ask")
        eff = T.Limits.for_task(make_task(merge="ask"), max_attempts=2, review_rounds=2, max_verify_fixes=2,
                                merge="auto", autoapprove="non_risky")
        self.check(tr("verifying", "verify_green", eff), "R31b", "awaiting_accept", attempts=0, text="merge=ask")

    def test_R32_conflict_below_cap_goes_to_conflict_fix(self):
        self.check(tr("verifying", "verify_conflict", task={"verify_fixes": 1, "accepted": True}, tip="t6"), "R32",
                   "fixing", attempts=0, fix_kind="conflict", verify_fixes=2, accepted=False,
                   fix_base="t6", text="verify_fixes 1 < max_verify_fixes 2")

    def test_R33_red_below_cap_goes_to_verify_fix(self):
        self.check(tr("verifying", "verify_red", task={"verify_fixes": 0, "accepted": True}, tip="t5"), "R33",
                   "fixing", attempts=0, fix_kind="verify", verify_fixes=1, accepted=False, fix_base="t5",
                   red_seen=True, text="verify_fixes 0 < max_verify_fixes 2")

    def test_R34a_red_at_cap_blocks_with_a_question(self):
        t = tr("verifying", "verify_red", task={"verify_fixes": 2, "accepted": True})
        self.assertEqual((t.rid, t.to_state, t.updates, t.effects, t.cause),
                         ("R34a", "blocked", {"attempts": 0, "asked_by": "verify", "cause": "cap", "accepted": False,
                           "red_seen": True}, (), "verify_fixes 2 >= max_verify_fixes 2"))

    def test_R34b_conflict_at_cap_blocks_with_a_question(self):
        t = tr("verifying", "verify_conflict", task={"verify_fixes": 2, "accepted": True})
        self.assertEqual((t.rid, t.to_state, t.updates, t.effects, t.cause),
                         ("R34b", "blocked", {"attempts": 0, "asked_by": "verify", "cause": "cap", "accepted": False}, (),
                          "verify_fixes 2 >= max_verify_fixes 2"))

    def test_R35_accept_sets_the_flag(self):
        self.check(tr("awaiting_accept", "accept", task={"flaky": True}), "R35", "verified", accepted=True, flaky=False,
                   stage_runs=0)

    def test_R36_fast_forward_done_deletes_branch(self):
        self.check(tr("verified", "ff_ok"), "R36", "done", effects=("delete_branch",))

    def test_R37_ff_impossible_reverifies_without_updates(self):
        self.check(tr("verified", "ff_impossible"), "R37", "verifying")


    def test_R17r_resets_like_R17(self):
        """Mutation caught: a missing row (TransitionError) or a branch kept after a write without commits."""
        fields = {"round": 2, "verify_fixes": 1, "attempts": 0, "fix_base": "abc", "accepted": True,
                  "review_closure": 2}
        reset = dict(round=0, verify_fixes=0, fix_kind=None, fix_base=None, accepted=False, review_closure=0)
        self.check(tr("writing", "ready_no_commits", task=fields), "R17r", "open", attempts=1, **reset,
                   effects=("reset_branch",), text="attempts 1 < max_attempts 2")
        self.check(tr("writing", "ready_no_commits", task=dict(fields, attempts=1)), "R17r", "stuck", attempts=2,
                   stuck_from="writing", **reset, effects=("reset_branch",), text="attempts 2 >= max_attempts 2")

    def test_verify_fix_timeout_retries_in_place(self):
        """Mutation caught: a failed verify fix reset to R17 (the branch and its fix-round state thrown away)."""
        t = tr("fixing", "stage_failed", task={"fix_kind": "verify", "attempts": 0})
        self.assertEqual((t.rid, t.to_state), ("R17f", "fixing"))

    def test_R17f_fix_failure_retries_in_place(self):
        """Mutation caught: the old R17 reset for a fixing task (a reset_branch effect, round/verify_fixes zeroed)."""
        fields = {"fix_kind": "review", "round": 1, "verify_fixes": 1, "attempts": 0, "fix_base": "abc",
                  "accepted": True, "review_closure": 1}
        self.check(tr("fixing", "stage_failed", task=fields), "R17f", "fixing", attempts=1,
                   text="attempts 1 < max_attempts 2")
        self.check(tr("fixing", "stage_failed", lim(max_attempts=5), task=dict(fields, attempts=3)), "R17f",
                   "fixing", attempts=4, text="attempts 4 < max_attempts 5")

    def test_R17g_resets_once(self):
        """Mutation caught: no reset when attempts are spent (`attempts_spent`), a missing `resets+1`, a reset that
        keeps the fix-round state, `accepted` or the closure kind."""
        fields = {"fix_kind": "verify", "round": 2, "verify_fixes": 1, "attempts": 1, "fix_base": "abc",
                  "accepted": True, "review_closure": 2, "resets": 0}
        self.check(tr("fixing", "stage_failed", task=fields), "R17g", "open", attempts=0, resets=1, round=0,
                   verify_fixes=0, fix_kind=None, fix_base=None, accepted=False, review_closure=0,
                   effects=("reset_branch",),
                   text="attempts 2 >= max_attempts 2: branch reset 1 of max_branch_resets 1")

    def test_R17h_stuck_after_reset(self):
        """Mutation caught: a second reset (R17g without `resets_left`), the stuck_from of the source state, a
        reset_branch effect on the way to stuck (the human gets the branch as the fixer left it)."""
        fields = {"fix_kind": "review", "round": 1, "attempts": 1, "resets": 1}
        t = tr("fixing", "stage_failed", task=fields)
        self.check(t, "R17h", "stuck", attempts=2, stuck_from="fixing",
                   text="fix failed 2 times after 1 branch reset(s) (max_branch_resets=1)")
        self.assertNotIn("reset_branch", t.effects)

    def test_max_branch_resets_zero_goes_stuck_at_once(self):
        """Mutation caught: `resets_left` treating 0 as unlimited."""
        t = tr("fixing", "stage_failed", lim(max_branch_resets=0), task={"fix_kind": "review", "attempts": 1})
        self.assertEqual((t.rid, t.to_state), ("R17h", "stuck"))

    def test_R17_is_for_writers_only(self):
        """Mutation caught: R17 left on `fixing` (it would be ambiguous with R17f/g/h)."""
        self.assertEqual({r.rid: r.sources for r in T.TABLE if r.rid in ("R17", "R17r")},
                         {"R17": ("writing",), "R17r": ("writing",)})

    def test_R29b_rework_resets_resets(self):
        """Mutation caught: a human rework that leaves `resets` at its cap (the next failed fix goes stuck at once)."""
        t = tr("stuck", "rework", task={"stuck_from": "fixing", "resets": 1, "attempts": 2}, has_branch=True, tip="t")
        self.assertEqual(t.rid, "R29b")
        self.assertEqual(t.updates["resets"], 0)

    def test_R32_records_fix_base(self):
        """Mutation caught: R32 without `fix_base` (the conflict-resolution review would see the whole branch)."""
        self.assertEqual(tr("verifying", "verify_conflict", tip="t7").updates["fix_base"], "t7")

    def test_R31c_flaky_green_awaits_even_when_accepted(self):
        """Mutation caught: a flaky green with `accepted` routed to `verified` (R31a's accepted guard), a missing
        `flaky=1`, or no `attempts=0`."""
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                self.check(tr("verifying", "verify_green_flaky", lim(merge="auto"),
                              task={"accepted": accepted, "attempts": 1}), "R31c", "awaiting_accept",
                           flaky=True, attempts=0)

    def test_R38_requeues_without_counter(self):
        """Mutation caught: a counter on the requeue (an unavailable agent must not cost an attempt), a changed
        state, a missing source stage."""
        for state in T.STAGE_STATES:
            with self.subTest(state=state):
                self.check(tr(state, "agent_unavailable", task={"attempts": 1, "stage_runs": 3}), "R38", state,
                           effects=("requeue",))

    def test_R39_due_at_cap_goes_stuck(self):
        """Mutation caught: a stuck below the cap, the wrong stuck_from, a missing cause."""
        for state in T.STAGE_STATES:
            with self.subTest(state=state):
                self.check(tr(state, "due_at_cap", lim(max_stage_runs=12), task={"stage_runs": 12}), "R39", "stuck",
                           stuck_from=state, text="12 agent runs since a human last moved the task (max_stage_runs=12)")
        with self.assertRaises(T.TransitionError):
            tr("writing", "due_at_cap", lim(max_stage_runs=12), task={"stage_runs": 11})

    def test_R39_is_a_row(self):
        """Mutation caught: the cap check done in the runner instead of the table (N10 b)."""
        row = {r.rid: r for r in T.TABLE}["R39"]
        self.assertEqual((row.event, row.to, row.when, row.cause), ("due_at_cap", "stuck", ("stage_runs_spent",),
                                                                     "max_stage_runs"))

    def test_R14b_clears_review_closure(self):
        """Mutation caught: R14b leaving the closure kind R29 kept (the review after a human rework would run the
        narrow conflict or verify-fix template)."""
        for closure in (1, 2):
            with self.subTest(review_closure=closure):
                blocked = tr("blocked", "rework", task={"asked_by": "review", "cause": "cap", "review_closure": closure},
                             has_branch=True, tip="t")
                self.assertEqual(blocked.rid, "R29a")
                after = tr("fixing", "ready", task={"fix_kind": "review", "review_closure": closure, "round": 1})
                self.assertEqual((after.rid, after.updates["review_closure"]), ("R14b", 0))

    def test_R35_accept_clears_flaky(self):
        """Mutation caught: R35 keeping `flaky` (a later flaky label on a task a human accepted)."""
        self.assertIs(tr("awaiting_accept", "accept", task={"flaky": True}).updates["flaky"], False)

    def test_red_rows_set_red_seen(self):
        """Mutation caught: a red verify (R33, R34a) that does not remember it was red (PD-72)."""
        for rid, fixes in (("R33", 0), ("R34a", 2)):
            with self.subTest(row=rid):
                t = tr("verifying", "verify_red", task={"verify_fixes": fixes}, tip="t")
                self.assertEqual((t.rid, t.updates["red_seen"]), (rid, True))
        self.assertNotIn("red_seen", tr("verifying", "verify_conflict", tip="t").updates)

    def test_human_rows_reset_stage_runs(self):
        """Mutation caught: any answer/rework/approve/accept row left out of the reset (spec §3.1 Counters)."""
        listed = ("R08", "R23", "R24", "R25", "R26", "R27", "R28", "R28a", "R29a", "R29b", "R30", "R35", "R35b")
        rows = {r.rid: r for r in T.TABLE}
        for rid in listed:
            with self.subTest(row=rid):
                self.assertIn("stage_runs=0", rows[rid].sets)
        self.assertEqual({r.rid for r in T.TABLE if "stage_runs=0" in r.sets}, set(listed))   # and no other row

    def test_non_human_rows_keep_stage_runs(self):
        """Mutation caught: a success row (R14a, R21, R31a ...) zeroing the counter the night safety relies on."""
        for t in (tr("writing", "ready"), tr("reviewing", "review_clear"), tr("verifying", "verify_green"),
                  tr("fixing", "stage_failed", task={"attempts": 0}), tr("writing", "stage_failed")):
            self.assertNotIn("stage_runs", t.updates, t.rid)

    def test_R15b_blocks_keeps_branch(self):
        """Mutation caught: the R17r reset for a verify fix that changed nothing (PD-53)."""
        t = tr("fixing", "ready_no_commits", task={"fix_kind": "verify", "attempts": 1})
        self.check(t, "R15b", "blocked", asked_by="verify", cause="ask")
        self.assertNotIn("reset_branch", t.effects)
        self.assertNotIn("attempts", t.updates)

    def test_R15b_only_for_verify_fixes(self):
        for kind in ("review", "conflict", None):
            with self.subTest(fix_kind=kind), self.assertRaises(T.TransitionError):
                tr("fixing", "ready_no_commits", task={"fix_kind": kind})

    def test_answer_after_R15b_is_R28a_without_cap_change(self):
        """Mutation caught: the old R28 bumping extra_verify_fixes for an answer to 'fixer made no change'."""
        t = tr("blocked", "answer", task={"asked_by": "verify", "cause": "ask", "extra_verify_fixes": 0}, tip="t9")
        self.check(t, "R28a", "fixing", fix_kind="verify", fix_base="t9", effects=("append_answer",), **HUMAN)
        self.assertNotIn("extra_verify_fixes", t.updates)

    def test_answer_after_cap_is_R28_with_cap_change(self):
        t = tr("blocked", "answer", task={"asked_by": "verify", "cause": "cap", "extra_verify_fixes": 1}, tip="t9")
        self.check(t, "R28", "fixing", fix_kind="verify", extra_verify_fixes=2, fix_base="t9",
                   effects=("append_answer",), **HUMAN)

    def test_accept_after_R15b_is_R35b(self):
        """Mutation caught: accept limited to awaiting_accept; an accept that sets `accepted` and bypasses the gates."""
        t = tr("blocked", "accept", task={"asked_by": "verify", "cause": "ask"})
        self.check(t, "R35b", "verifying", fix_kind=None, **HUMAN)
        self.assertNotIn("accepted", t.updates)

    def test_accept_on_review_blocked_is_an_error(self):
        for asked_by, cause in (("review", "cap"), ("review", "ask"), ("verify", "cap"), ("write", "ask")):
            with self.subTest(asked_by=asked_by, cause=cause), self.assertRaises(T.TransitionError):
                tr("blocked", "accept", task={"asked_by": asked_by, "cause": cause})

    def test_success_rows_reset_attempts(self):
        """Mutation caught: any listed row without `attempts=0` (PD-49)."""
        listed = ["R04a", "R04b", "R04c", "R04d", "R05", "R07a", "R07b", "R11", "R12", "R14a", "R14b", "R15",
                  "R16", "R21", "R22a", "R22b", "R31a", "R31b", "R31c", "R32", "R33", "R34a", "R34b"]
        rows = {r.rid: r for r in T.TABLE}
        for rid in listed:
            with self.subTest(row=rid):
                self.assertIn("attempts=0", rows[rid].sets)

    def test_non_success_rows_keep_attempts(self):
        """Mutation caught: resetting on ask, a human event, or a failure below the cap."""
        keep = [("fixing", "ask", {"fix_kind": "verify"}), ("blocked", "answer", {"asked_by": "review", "cause": "ask"}),
                ("blocked", "answer", {"asked_by": "verify", "cause": "cap"}),
                ("blocked", "answer", {"asked_by": "verify", "cause": "ask"}),
                ("verifying", "setup_failed", {}), ("awaiting_accept", "accept", {}),
                ("blocked", "accept", {"asked_by": "verify", "cause": "ask"}),
                ("fixing", "ready_no_commits", {"fix_kind": "verify"})]
        for state, event, fields in keep:
            with self.subTest(state=state, event=event, **fields):
                t = tr(state, event, task=dict(fields, attempts=1), tip="x", stage="fix" if event == "ask" else None)
                self.assertNotEqual(t.updates.get("attempts"), 0)
        rows = {r.rid: r for r in T.TABLE}
        for rid in ("R13", "R17", "R18", "R19", "R20", "R23", "R24", "R25", "R26", "R27", "R28", "R28a", "R35",
                    "R35b", "R36", "R37", "R17f", "R38", "R39"):
            self.assertNotIn("attempts=0", rows[rid].sets, rid)

    def test_R30_only_for_codeless_stuck_stages(self):
        """Mutation caught: the old unrestricted R30 (a reviewing/fixing/verifying task without a branch)."""
        for stage in ("reviewing", "fixing", "verifying"):
            with self.subTest(stage=stage), self.assertRaises(T.TransitionError):
                tr("stuck", "rework", task={"stuck_from": stage}, has_branch=False)
        for stage in ("vetting", "attacking", "writing"):
            self.assertEqual(tr("stuck", "rework", task={"stuck_from": stage}, has_branch=False).to_state, stage)


class MetaTests(unittest.TestCase):
    def test_every_row_has_a_test(self):
        names = {n for n in dir(RowTests) if n.startswith("test_")}
        for row in T.TABLE:
            self.assertTrue(any(re.search(rf"(^|_){row.rid}(_|$)", n[5:]) for n in names), row.rid)

    def test_table_rows_are_unambiguous(self):
        samples = []
        for fix_kind, then, asked_by, cause, accepted, rejected_by in itertools.product(
                (None, "review", "verify", "conflict"), (None, "open", "awaiting_approval"),
                (None, "vet", "attack", "write", "review", "fix", "verify"), (None, "ask", "cap"),
                (False, True), (None, "vet", "human")):
            samples.append(dict(fix_kind=fix_kind, then=then, asked_by=asked_by, cause=cause,
                                accepted=accepted, rejected_by=rejected_by))
        counters = [dict(round=r, verify_fixes=v, attempts=a, attack=k, stage_runs=n, resets=x) for r, v, a, k, n, x in
                    itertools.product((0, 1, 2, 3), (0, 1, 2, 3), (0, 1, 2), (False, True), (0, 12), (0, 1))]
        limits = [lim(autoapprove=a, merge=m) for a in ("none", "non_risky", "all") for m in ("auto", "ask")]
        checked = 0
        for state in B.STATES:
            for event in T.EVENTS:
                for i, fields in enumerate(samples[::5]):
                    extra = counters[i % len(counters)]
                    for has_branch, limit in ((True, limits[i % 6]), (False, limits[(i + 1) % 6])):
                        task = make_task(state=state, stuck_from=("writing", "reviewing")[i % 2], **fields, **extra)
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

    def test_extra_sets_applied_in_same_cas(self):
        """Mutation caught: extra_sets applied before the row's own sets (a row's reset would win), applied in a
        second update (two versions, a window where the stage is not counted), or ignored by `decide`."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            b = B.open_board(Path(d) / "b.db")
            self.addCleanup(b.close)
            tid = T.create(b, "add_human", title="t")
            b.update_task(tid, expect_state="open", expect_version=0, stage_runs=4)
            before = b.get_task(tid).version
            tr_ = T.apply(b, tid, "pick", LIM, extra_sets=("stage_runs+1",))
            task = b.get_task(tid)
            self.assertEqual((tr_.rid, task.stage_runs, task.version), ("R10b", 5, before + 1))
            self.assertEqual(tr_.updates, {"stage_runs": 5})
        # the caller's sets come after the row's own: a later spec wins
        t = tr("fixing", "stage_failed", task={"attempts": 0, "stage_runs": 2}, extra_sets=("stage_runs+1",))
        self.assertEqual((t.rid, t.updates["attempts"], t.updates["stage_runs"]), ("R17f", 1, 3))
        t = tr("blocked", "answer", task={"asked_by": "vet", "cause": "ask", "stage_runs": 7},
               extra_sets=("stage_runs+1",))
        self.assertEqual(t.updates["stage_runs"], 8)
        t = tr("fixing", "stage_failed", task={"attempts": 0}, extra_sets=("attempts=9",))
        self.assertEqual(t.updates["attempts"], 9)   # applied after the row's own sets, so it wins

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
               "autoapprove": "none", "verify_reruns": 2, "max_stage_runs": 9, "max_branch_resets": 3,
               "unrelated": 1}
        got = T.Limits.from_cfg(make_task(extra_verify_fixes=1), cfg)
        self.assertEqual(got, T.Limits(5, 3, 2, "ask", "none", verify_reruns=2, max_stage_runs=9,
                                       max_branch_resets=3))

    def test_limits_defaults_keep_old_callers(self):
        """Mutation caught: the new limits as required fields (Tasks 15/18 build Limits with five arguments)."""
        old = T.Limits(2, 2, 2, "auto", "non_risky")
        self.assertEqual((old.verify_reruns, old.max_stage_runs, old.max_branch_resets), (1, 12, 1))
        got = T.Limits.for_task(make_task(), max_attempts=2, review_rounds=2, max_verify_fixes=2, merge="auto",
                                autoapprove="non_risky")
        self.assertEqual(got, old)

    test_for_task_old_keywords_still_work = test_limits_defaults_keep_old_callers

    def test_agent_task_cannot_loosen_merge(self):
        """Mutation caught: the old `task.merge or merge` for an agent-created task (it could set merge=auto over a
        configured ask and merge its own work unattended)."""
        agent = make_task(created_by="agent:3", merge="auto")
        human = make_task(created_by="human", merge="auto")
        kw = dict(max_attempts=2, review_rounds=2, max_verify_fixes=2, autoapprove="none")
        self.assertEqual(T.Limits.for_task(agent, merge="ask", **kw).merge, "ask")
        self.assertEqual(T.Limits.for_task(human, merge="ask", **kw).merge, "auto")
        self.assertEqual(T.Limits.for_task(make_task(created_by="agent:3", merge="ask"), merge="auto", **kw).merge, "ask")
        self.assertEqual(T.Limits.for_task(make_task(created_by="agent:3"), merge="auto", **kw).merge, "auto")

    def test_agent_task_cannot_lower_review_rounds(self):
        """Mutation caught: an agent-created task with review_rounds below the configuration (or 0)."""
        kw = dict(max_attempts=2, max_verify_fixes=2, merge="auto", autoapprove="none")
        for own, expected_agent, expected_human in ((0, 3, 0), (1, 3, 1), (5, 5, 5), (None, 3, 3)):
            with self.subTest(own=own):
                agent = T.Limits.for_task(make_task(created_by="agent:3", review_rounds=own), review_rounds=3, **kw)
                human = T.Limits.for_task(make_task(created_by="human", review_rounds=own), review_rounds=3, **kw)
                self.assertEqual((agent.review_rounds, human.review_rounds), (expected_agent, expected_human))
        extra = T.Limits.for_task(make_task(created_by="agent:3", review_rounds=1, extra_review_rounds=2),
                                  review_rounds=3, **kw)
        self.assertEqual(extra.review_rounds, 5)   # a human's extra rounds still add

    def test_rows_entering_fixing_set_fix_base(self):
        for row in T.TABLE:
            if row.to == "fixing":
                self.assertIn("fix_base=tip", row.sets, row.rid)   # R32 too: the resolution review sees fix_base.. (PD-67)

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
        """Mutation caught: R17g not clearing review_closure (a fresh review would run as a closure, PD-4)."""
        task = make_task(state="fixing", fix_kind="conflict", round=2)
        t = T.decide(task, "ready", LIM)                                     # R16: closure review
        self.assertEqual((t.rid, t.updates["review_closure"]), ("R16", True))
        task = dataclasses.replace(task, state="reviewing", fix_kind=None, review_closure=1)
        t = T.decide(task, "ask", LIM, stage="review")                       # reviewer asks
        task = dataclasses.replace(task, state="blocked", asked_by="review", cause="ask")
        t = T.decide(task, "rework", LIM, has_branch=True, tip="t")          # R29a -> fixing
        self.assertEqual(t.rid, "R29a")
        task = dataclasses.replace(task, state="fixing", fix_kind="review", asked_by=None, cause=None, attempts=1)
        t = T.decide(task, "stage_failed", LIM)                              # R17g: branch reset
        self.assertEqual((t.rid, t.to_state), ("R17g", "open"))
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
