"""Task 35: the runner's limit, quota and agent_unavailable pauses, the night budget and the probe job (spec §5 step 5,
§3.1 Pauses; PD-22, PD-40, PD-54..PD-60)."""
import json

from unittest import mock

from artel import pauses, probe
from artel.agent_claude import RateLimit, status_rank
from artel.events import Event
from tests.e2e import E2ECase

MIN = 60.0
HOUR = 3600.0
DAY = 24 * HOUR
T0 = 1_700_000_000.0
WRITE = [{"write": ["hello.txt", "hi\n"]}, {"commit": "add hello"},
         {"result": {"outcome": "ready", "summary": "done"}}]
PROBE_OK = [{"assistant": {"input_tokens": 1, "output_tokens": 1}}, {"result": {"summary": "OK"}}]
PROBE_429 = [{"result_error": 429}]
PROBE_DOWN = [{"exit": 1}]


def limit_event(status="rejected", resets_at=None, limit_type="five_hour", utilization=None):
    windows = {}
    if limit_type and utilization is not None:
        windows[limit_type] = {"utilization": utilization, "resets_at": resets_at}
    return RateLimit(status=status, rank=status_rank(status), resets_at=resets_at, limit_type=limit_type,
                     utilization=utilization, windows=windows)


def week(utilization, resets_at=None):
    return limit_event("allowed", resets_at, "seven_day", utilization)


class LimitCase(E2ECase):
    SETTINGS = dict(quota_stop_five_hour=0.95, quota_stop_seven_day=0.8, night_budget_seven_day=0.15,
                    limit_retry="30m", limit_probes=4, max_agents=1)

    def started(self, **kw):
        r = self.runner(**{**self.SETTINGS, **kw})
        r.start()
        return r

    def active(self):
        return pauses.active(self.board)

    def kinds(self):
        return sorted(self.active())

    def sent(self, r, kind=None):
        self.assertTrue(r.flush_notifications(10), "notifications not delivered")
        return [e for e in self.events if kind is None or e.kind == kind]

    def feed(self, r, *events):
        for ev in events:
            r.on_rate_limit(ev)
        r.tick()

    def probe_calls(self):
        return [c for c in self.calls() if c["stage"] == "probe"]

    def finish_probe(self, r, n):
        """Ticks until n probes are recorded and none is running."""
        self.drive(r, lambda: len(pauses.probe_times(self.board, now=self.clock())) >= n
                   and not any(j.kind == "probe" for j in r.jobs))

    def probes_at(self, r, *offsets):
        """Advances the clock to each offset from T0 and expects a probe to have run exactly then."""
        for i, offset in enumerate(offsets, start=1):
            self.clock.now = T0 + offset - 1
            r.tick()
            self.assertEqual(len(self.probe_calls()), i - 1, f"a probe ran before +{offset / MIN:g} min")
            self.clock.now = T0 + offset
            self.finish_probe(r, i)
            self.assertEqual(len(self.probe_calls()), i)


class RateLimitEventTest(LimitCase):
    def test_rate_limit_warning_pauses_new_picks_until_reset(self):
        """Mutation caught: a warning that does not pause; a pause without `until`; a pause that stops the running
        stage; no paused/resumed pair."""
        self.scenario({"write": [{"rate_limit": ["allowed_warning", T0 + 2, "five_hour", 0.5]}, *WRITE]})
        r = self.started()
        first = self.add_task("first")
        self.add_task("second")
        self.drive(r, lambda: "limit" in self.active())
        self.assertEqual(self.active()["limit"].until, T0 + 2)
        self.drive(r, lambda: self.board.get_task(first).state != "writing" and not r.jobs)
        self.assertEqual(len([c for c in self.calls() if c["stage"] == "write"]), 1)   # no new pick while paused
        self.clock.advance(3)
        r.tick()
        self.assertNotIn("limit", self.active())
        kinds = [(e.kind, e.text.split(":")[0]) for e in self.sent(r) if e.kind in ("paused", "resumed")]
        self.assertEqual(kinds, [("paused", "limit"), ("resumed", "limit")])

    def test_unknown_status_treated_as_warning(self):
        """PD-22. Mutation caught: only `rejected` pausing."""
        r = self.started()
        self.feed(r, limit_event("something_new", T0 + HOUR))
        self.assertIn("limit", self.active())

    def test_allowed_status_does_not_pause(self):
        r = self.started()
        self.feed(r, limit_event("allowed", T0 + HOUR, utilization=0.5))
        self.assertEqual(self.kinds(), [])

    def test_limit_stop_rejected_ignores_warning(self):
        """Mutation caught: ignoring the limit_stop setting."""
        r = self.started(limit_stop="rejected")
        self.feed(r, limit_event("allowed_warning", T0 + HOUR))
        self.assertEqual(self.kinds(), [])
        self.feed(r, limit_event("rejected", T0 + HOUR))
        self.assertEqual(self.kinds(), ["limit"])

    def test_rate_limit_rejected_requeues_without_counter(self):
        """Mutation caught: charging attempts or the failure streak for a rejected stage."""
        self.scenario({"write": [{"rate_limit": ["rejected", T0 + HOUR, "five_hour", 1.0]}, {"result_error": 429}]})
        r = self.started()
        tid = self.add_task("limited")
        self.drive(r, lambda: "limit" in self.active() and not r.jobs)
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.attempts, task.running), ("writing", 0, False))   # requeued in place
        self.assertIsNone(self.board.get_meta("failure_streak"))
        self.assertEqual(self.active()["limit"].until, T0 + HOUR)

    def test_limit_without_reset_uses_recorded_window(self):
        """Mutation caught: ignoring the recorded usage windows (the pause would wait for a probe)."""
        r = self.started()
        self.feed(r, limit_event("allowed", T0 + 3 * HOUR, "five_hour", 0.95))
        self.feed(r, limit_event("rejected", None, None))
        self.assertEqual(self.active()["limit"].until, T0 + 3 * HOUR)

    def test_limit_without_any_reset_waits_for_a_probe(self):
        r = self.started()
        self.feed(r, limit_event("rejected", None, None))
        self.assertIsNone(self.active()["limit"].until)

    def test_429_error_result_pauses_and_requeues(self):
        """PD-40: the limit shown only by the error result."""
        self.scenario({"write": [{"result_error": 429}]})
        r = self.started()
        tid = self.add_task("limited")
        self.drive(r, lambda: "limit" in self.active() and not r.jobs)
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.attempts, task.running), ("writing", 0, False))

    def test_rate_limit_meta_is_json(self):
        """Task 4 format. Mutation caught: a repr or a bare status in `limit:<type>`."""
        r = self.started()
        self.feed(r, limit_event("allowed", T0 + HOUR, "five_hour", 0.4))
        data = json.loads(self.board.get_meta("limit:five_hour"))
        self.assertEqual((data["status"], data["rank"], data["resets_at"], data["utilization"]),
                         ("allowed", 0, T0 + HOUR, 0.4))


class ProbeScheduleTest(LimitCase):
    def test_limit_without_any_reset_probes_on_schedule(self):
        """Probes at +30, +90, +180, +300 min; an ok probe lifts and notifies. Mutation caught: waiting for a human
        at once; a constant gap."""
        self.scenario({":probe": PROBE_429})
        r = self.started(limit_probes=5)
        self.feed(r, limit_event("rejected", None, None))
        self.probes_at(r, 30 * MIN, 90 * MIN, 180 * MIN, 300 * MIN)
        self.scenario({":probe": PROBE_OK})
        self.clock.now = T0 + 300 * MIN + 150 * MIN
        self.finish_probe(r, 5)
        self.assertNotIn("limit", self.active())
        self.assertIn(("resumed", "limit"), [(e.kind, e.text) for e in self.sent(r)])

    def test_clock_jump_runs_one_probe(self):
        """Mutation caught: anchoring at `since` (a burst after a long sleep)."""
        self.scenario({":probe": PROBE_429})
        r = self.started()
        self.feed(r, limit_event("rejected", None, None))
        self.clock.now = T0 + 8 * HOUR
        self.finish_probe(r, 1)
        r.tick()
        r.tick()
        self.assertEqual(len(self.probe_calls()), 1)
        self.clock.now = T0 + 8 * HOUR + 59 * MIN
        r.tick()
        self.assertEqual(len(self.probe_calls()), 1)
        self.clock.now = T0 + 9 * HOUR
        self.finish_probe(r, 2)
        self.assertEqual(len(self.probe_calls()), 2)

    def test_probes_capped_per_24h_then_exhausted(self):
        """limit and agent_unavailable share `limit_probes`. Mutation caught: a per-kind cap; the cap kept in the
        pause record."""
        self.scenario({":probe": PROBE_429})
        r = self.started()
        r.pause("limit", "usage limit")
        r.pause("agent_unavailable", "agent down")
        for i in range(1, 5):
            self.clock.now = T0 + i * 30 * MIN * i
            self.finish_probe(r, i)
        self.clock.now = T0 + 20 * HOUR
        r.tick()
        r.tick()
        self.assertEqual(len(self.probe_calls()), 4)
        self.assertEqual({k: p.exhausted for k, p in self.active().items()},
                         {"limit": True, "agent_unavailable": True})
        texts = [e.text for e in self.sent(r, "paused") if "artel resume" in e.text]
        self.assertEqual(len(texts), 2)
        self.clock.now = T0 + 23 * HOUR
        r.tick()
        self.assertEqual(len(self.probe_calls()), 4)

    def test_probe_cap_survives_pause_removal(self):
        """Mutation caught: the count dropped with the pause record."""
        self.scenario({":probe": PROBE_429})
        r = self.started()
        r.pause("limit", "usage limit")
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        self.clock.now = T0 + 90 * MIN
        self.finish_probe(r, 2)
        r.unpause("limit")
        self.clock.now = T0 + 2 * HOUR
        r.pause("limit", "usage limit")
        self.clock.now = T0 + 2 * HOUR + 30 * MIN
        self.finish_probe(r, 3)
        self.clock.now = T0 + 2 * HOUR + 90 * MIN
        self.finish_probe(r, 4)
        self.clock.now = T0 + 5 * HOUR
        r.tick()
        r.tick()
        self.assertEqual(len(self.probe_calls()), 4)
        self.assertTrue(self.active()["limit"].exhausted)

    def test_readded_pause_restarts_k(self):
        """Mutation caught: counting the probes of an earlier pause toward k."""
        self.scenario({":probe": PROBE_429})
        r = self.started()
        r.pause("limit", "usage limit")
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        r.unpause("limit")
        self.clock.now = T0 + 40 * MIN
        r.pause("limit", "usage limit")
        self.clock.now = T0 + 40 * MIN + 29 * MIN
        r.tick()
        self.assertEqual(len(self.probe_calls()), 1)
        self.clock.now = T0 + 70 * MIN
        self.finish_probe(r, 2)

    def test_probe_runs_as_job_main_thread_keeps_ticking(self):
        """Mutation caught: a blocking probe (a stop request would wait for it)."""
        self.scenario({":probe": [{"sleep": 5}, *PROBE_OK]})
        r = self.started()
        r.pause("limit", "usage limit")
        self.clock.now = T0 + 30 * MIN
        r.tick()
        self.assertTrue(any(j.kind == "probe" for j in r.jobs))
        r.pause("manual", "x")
        self.assertIn("manual", self.active())            # ticking and pausing worked while the probe slept
        r.tick()
        self.assertEqual(len([j for j in r.jobs if j.kind == "probe"]), 1)   # at most one at a time
        r.request_stop(now=True)
        self.drive(r, lambda: not r.jobs, timeout=20)

    def test_agent_unavailable_pause_lifted_by_probe(self):
        """The classifier is Task 37; the pause is added directly."""
        self.scenario({":probe": PROBE_OK})
        r = self.started()
        r.pause("agent_unavailable", "agent down")
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        self.assertEqual(self.kinds(), [])

    def test_two_probed_kinds_keep_own_schedule(self):
        """Mutation caught: one shared `last_probe`."""
        self.scenario({":probe": PROBE_429})
        r = self.started(limit_probes=9)
        r.pause("limit", "usage limit")
        self.clock.now = T0 + 10 * MIN
        r.pause("agent_unavailable", "agent down")
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)                                  # limit's first
        self.clock.now = T0 + 40 * MIN
        self.finish_probe(r, 2)                                  # agent_unavailable's first (10 + 30)
        self.clock.now = T0 + 89 * MIN
        r.tick()
        self.assertEqual(len(self.probe_calls()), 2)
        self.clock.now = T0 + 90 * MIN
        self.finish_probe(r, 3)                                  # limit's second: 30 + 60
        self.assertEqual([k for _, k in pauses.probe_times(self.board, now=self.clock())],
                         ["limit", "agent_unavailable", "limit"])

    def test_probe_never_counts(self):
        """Mutation caught: a probe writing a runs row, an attempt or a failure."""
        self.scenario({":probe": PROBE_429})
        r = self.started()
        tid = self.add_task("waiting")
        r.pause("limit", "usage limit")
        before = (self.board.get_task(tid).attempts, self.board.get_meta("failure_streak"), len(self.board.all_runs()))
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        after = (self.board.get_task(tid).attempts, self.board.get_meta("failure_streak"), len(self.board.all_runs()))
        self.assertEqual(before, after)
        self.assertEqual(self.board.get_task(tid).state, "open")

    def test_probe_env_has_artel_child(self):
        """PD-65 a. Mutation caught: the probe started with the bare environment."""
        self.scenario({":probe": PROBE_OK})
        r = self.started()
        r.pause("limit", "usage limit")
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        (call,) = self.probe_calls()
        self.assertEqual(call["env"].get("ARTEL_CHILD"), "1")

    def test_probed_limit_learns_its_reset_without_a_second_notification(self):
        """Mutation caught: the probe's reset time ignored for a `limit`; a second `paused` for the same pause."""
        self.scenario({":probe": [{"rate_limit": ["rejected", T0 + 6 * HOUR, "five_hour", 0.5]}, {"result_error": 429}]})
        r = self.started()
        self.feed(r, limit_event("rejected", None, None))
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        self.assertEqual(self.active()["limit"].until, T0 + 6 * HOUR)
        self.assertEqual(len(self.sent(r, "paused")), 1)
        self.assertEqual(len(self.probe_calls()), 1)
        self.clock.now = T0 + 6 * HOUR
        r.tick()
        self.assertEqual(self.kinds(), [])

    def test_probed_pause_waits_while_a_limit_has_a_known_end(self):
        """Review finding: the probed pause kept being probed (and exhausted) while the limit's end was known.
        Mutation caught: no skip."""
        self.scenario({":probe": [{"rate_limit": ["rejected", T0 + 6 * HOUR, "five_hour", 0.5]}, {"result_error": 429}]})
        r = self.started()
        r.pause("agent_unavailable", "agent down")
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        self.clock.now = T0 + 5 * HOUR
        self.drive(r, lambda: not r.jobs)
        self.assertEqual(len(self.probe_calls()), 1)
        self.assertFalse(self.active()["agent_unavailable"].exhausted)
        self.clock.now = T0 + 6 * HOUR + 30 * MIN
        self.finish_probe(r, 2)                        # the limit ended: probing goes on

    def test_stop_request_starts_no_probe_and_a_stopped_probe_is_counted(self):
        """Review finding + controller decision: a stopped probe counts. Mutations caught: probing while stopping; not
        counting a stopped probe (it would be due again at once after a restart)."""
        self.scenario({":probe": [{"sleep": 30}, *PROBE_OK]})
        r = self.started()
        r.pause("limit", "usage limit")
        self.clock.now = T0 + 30 * MIN
        r.tick()
        self.wait_until(lambda: self.probe_calls())
        r.request_stop(now=True)
        self.drive(r, lambda: not r.jobs, timeout=20)
        self.assertEqual(len(pauses.probe_times(self.board, now=self.clock())), 1)
        r.shutdown()
        r2 = self.started()
        r2.request_stop(now=False)
        self.clock.now = T0 + 5 * HOUR
        r2.tick()
        self.assertEqual([j for j in r2.jobs if j.kind == "probe"], [])

    def test_event_reset_in_the_past_is_not_used(self):
        """Review finding. Mutation caught: a pause until a past time (a paused/resumed pair per tick)."""
        r = self.started()
        self.feed(r, limit_event("rejected", T0 - 10))
        self.assertIsNone(self.active()["limit"].until)

    def test_probe_usage_over_threshold_pauses_quota(self):
        """Review finding. Mutation caught: a probe's windows stored but not evaluated."""
        self.scenario({":probe": [{"assistant": {"input_tokens": 1, "output_tokens": 1}},
                                  {"rate_limit": ["allowed", T0 + 3 * DAY, "seven_day", 0.9]},
                                  {"result": {"summary": "OK"}}]})
        r = self.started()
        r.pause("agent_unavailable", "agent down")
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        self.assertEqual(self.kinds(), ["quota"])

    def test_probe_limit_during_unavailable_adds_limit(self):
        """Mutation caught: the limit dropped, or replacing the probed pause."""
        self.scenario({":probe": [{"rate_limit": ["rejected", T0 + 6 * HOUR, "five_hour", 0.5]}, {"result_error": 429}]})
        r = self.started()
        r.pause("agent_unavailable", "agent down")
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        self.assertEqual(self.kinds(), ["agent_unavailable", "limit"])
        self.assertEqual(self.active()["limit"].until, T0 + 6 * HOUR)
        self.assertIsNone(self.active()["agent_unavailable"].until)


class QuotaTest(LimitCase):
    def test_quota_five_hour_threshold_pauses_until_reset(self):
        r = self.started()
        self.feed(r, limit_event("allowed", T0 + 2 * HOUR, "five_hour", 0.95))
        self.assertEqual(self.active()["quota"].until, T0 + 2 * HOUR)
        self.assertEqual(self.active()["quota"].causes[-1].setting, "quota_stop_five_hour")
        self.clock.now = T0 + 2 * HOUR
        r.tick()
        self.assertNotIn("quota", self.active())

    def test_quota_below_threshold_does_not_pause(self):
        r = self.started()
        self.feed(r, limit_event("allowed", T0 + 2 * HOUR, "five_hour", 0.94), week(0.79, T0 + DAY))
        self.assertEqual(self.kinds(), [])

    def test_quota_seven_day_threshold_pauses_until_reset(self):
        """Mutation caught: one threshold for both windows."""
        r = self.started()
        self.feed(r, week(0.8, T0 + 3 * DAY))
        self.assertEqual(self.active()["quota"].until, T0 + 3 * DAY)
        self.assertEqual(self.active()["quota"].causes[-1].setting, "quota_stop_seven_day")

    def test_second_quota_cause_extends_pause(self):
        """Mutation caught: losing the second cause (the pause would end at the first reset)."""
        r = self.started()
        self.feed(r, limit_event("allowed", T0 + 2 * HOUR, "five_hour", 0.97))
        self.feed(r, week(0.85, T0 + 3 * DAY))
        self.assertEqual(self.active()["quota"].until, T0 + 3 * DAY)
        self.clock.now = T0 + 2 * HOUR
        r.tick()
        self.assertIn("quota", self.active())

    def test_quota_without_future_reset_is_probed(self):
        """Mutation caught: a quota pause that never ends."""
        self.scenario({":probe": PROBE_OK})
        r = self.started()
        self.feed(r, limit_event("allowed", None, "five_hour", 0.97))
        self.assertIsNone(self.active()["quota"].until)
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        # the probe saw no window: a quota pause does not end on a bare ok
        self.assertIn("quota", self.active())

    def test_quota_probe_below_threshold_ends(self):
        self.scenario({":probe": [{"assistant": {"input_tokens": 1, "output_tokens": 1}},
                                  {"rate_limit": ["allowed", T0 + DAY, "five_hour", 0.2]},
                                  {"rate_limit": ["allowed", T0 + 3 * DAY, "seven_day", 0.3]},
                                  {"result": {"summary": "OK"}}]})
        r = self.started()
        self.feed(r, limit_event("allowed", None, "five_hour", 0.97))
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        self.assertNotIn("quota", self.active())

    def test_quota_probe_above_threshold_does_not_end(self):
        """Mutation caught: ending quota on any ok."""
        self.scenario({":probe": [{"assistant": {"input_tokens": 1, "output_tokens": 1}},
                                  {"rate_limit": ["allowed", T0 + 3 * DAY, "seven_day", 0.85]},
                                  {"result": {"summary": "OK"}}]})
        r = self.started()
        self.feed(r, week(0.9, None))
        self.assertIsNone(self.active()["quota"].until)
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        self.assertEqual(self.active()["quota"].until, T0 + 3 * DAY)

    def test_stored_window_with_past_reset_does_not_pause_a_new_runner(self):
        r = self.started()
        self.feed(r, week(0.9, T0 + HOUR))
        r.request_stop(now=True)
        r.shutdown()
        pauses.remove(self.board, "quota")
        self.clock.now = T0 + 2 * HOUR
        self.started()
        self.assertEqual(self.kinds(), [])

    def test_stored_window_without_reset_does_not_pause_a_new_runner(self):
        """Mutation caught: a stale record without a reset time pausing every new runner for ever."""
        r = self.started()
        self.feed(r, limit_event("allowed", None, "five_hour", 0.97))
        r.request_stop(now=True)
        r.shutdown()
        pauses.remove(self.board, "quota")
        self.started()
        self.assertEqual(self.kinds(), [])

    def test_usage_windows_survive_restart(self):
        """Mutation caught: windows kept only in memory — a new runner would start a stage."""
        self.scenario({"write": WRITE})
        r = self.started()
        self.feed(r, week(0.9, T0 + 3 * DAY))
        pauses.remove(self.board, "quota")
        r.request_stop(now=True)
        r.shutdown()
        r2 = self.started()
        self.assertEqual(self.active()["quota"].until, T0 + 3 * DAY)
        self.add_task("late")
        for _ in range(3):
            r2.tick()
        self.assertEqual([c for c in self.calls() if c["stage"] == "write"], [])


class NightBudgetTest(LimitCase):
    def test_night_budget_pauses_24h_after_baseline(self):
        """Mutation caught: a budget over a single sample; an end at now + 24 h instead of baseline + 24 h."""
        r = self.started()
        self.feed(r, week(0.30, T0 + 6 * DAY))
        self.clock.now = T0 + 20 * MIN
        self.feed(r, week(0.46, T0 + 6 * DAY))
        quota = self.active()["quota"]
        self.assertEqual(quota.until, T0 + DAY)
        self.assertEqual(quota.causes[-1].setting, "night_budget_seven_day")

    def test_night_budget_not_spent_does_not_pause(self):
        r = self.started()
        self.feed(r, week(0.30, T0 + 6 * DAY))
        self.clock.now = T0 + 20 * MIN
        self.feed(r, week(0.44, T0 + 6 * DAY))
        self.assertEqual(self.kinds(), [])

    def test_night_budget_ignores_samples_of_the_previous_week(self):
        """Mutation caught: samples kept across the weekly reset (a fall from 1.0 to 0.2 then a rise counted)."""
        r = self.started()
        self.feed(r, week(0.30, T0 + HOUR))
        self.clock.now = T0 + 2 * HOUR                    # the window rolled
        self.feed(r, week(0.46, T0 + 7 * DAY))
        self.assertEqual(self.kinds(), [])

    def test_night_budget_old_baseline_does_not_count(self):
        r = self.started()
        self.feed(r, week(0.30, T0 + 6 * DAY))
        self.clock.now = T0 + 25 * HOUR
        self.feed(r, week(0.50, T0 + 6 * DAY))
        self.assertEqual(self.kinds(), [])

    def test_night_budget_baseline_survives_restart(self):
        """Mutation caught: resetting the baseline at start."""
        r = self.started()
        self.feed(r, week(0.30, T0 + 6 * DAY))
        r.request_stop(now=True)
        r.shutdown()
        self.clock.now = T0 + 20 * MIN
        r2 = self.started()
        self.feed(r2, week(0.46, T0 + 6 * DAY))
        self.assertEqual(self.active()["quota"].until, T0 + DAY)


class OtherKindsTest(LimitCase):
    def test_limit_ending_keeps_base_conflict(self):
        """Mutation caught: a single pause slot."""
        r = self.started()
        r.pause("base_conflict", "diverged")
        self.feed(r, limit_event("rejected", T0 + HOUR))
        self.assertEqual(self.kinds(), ["base_conflict", "limit"])
        self.clock.now = T0 + HOUR
        r.tick()
        self.assertEqual(self.kinds(), ["base_conflict"])

    def test_manual_pause_without_until_is_never_probed(self):
        r = self.started()
        r.pause("manual", "x")
        self.clock.now = T0 + 10 * HOUR
        r.tick()
        r.tick()
        self.assertEqual(self.probe_calls(), [])


class ReviewFixesTest(LimitCase):
    """T35 cross-review (reviews/T35-1.md)."""

    def test_stale_quota_cause_dropped_when_its_window_falls_below(self):
        """M1. Mutation caught: a five_hour cause with until=None surviving the probe that found it low (the pause
        stayed open-ended, was probed until exhausted)."""
        self.scenario({":probe": [{"assistant": {"input_tokens": 1, "output_tokens": 1}},
                                  {"rate_limit": ["allowed", T0 + DAY, "five_hour", 0.5]},
                                  {"rate_limit": ["allowed", T0 + 3 * DAY, "seven_day", 0.85]},
                                  {"result": {"summary": "OK"}}]})
        r = self.started()
        self.feed(r, limit_event("allowed", None, "five_hour", 0.97))
        self.assertIsNone(self.active()["quota"].until)
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        quota = self.active()["quota"]
        self.assertEqual(quota.until, T0 + 3 * DAY)
        self.assertEqual([c.detail for c in quota.causes], ["seven_day window at or above quota_stop_seven_day"])

    def test_quota_ends_when_the_last_window_falls_below_on_an_event(self):
        r = self.started()
        self.feed(r, limit_event("allowed", T0 + 2 * HOUR, "five_hour", 0.97))
        self.feed(r, limit_event("allowed", T0 + 2 * HOUR, "five_hour", 0.3))
        self.assertEqual(self.kinds(), [])
        self.assertIn(("resumed", "quota"), [(e.kind, e.text) for e in self.sent(r)])

    def test_later_429_without_reset_keeps_a_known_limit_end(self):
        """M3a. Mutation caught: the reset-less event replacing the known end with the weekly reset."""
        r = self.started()
        self.feed(r, week(0.4, T0 + 5 * DAY))
        self.feed(r, limit_event("rejected", T0 + 2 * HOUR))
        self.feed(r, limit_event("rejected", None, None))
        self.assertEqual(self.active()["limit"].until, T0 + 2 * HOUR)

    def test_fresh_window_over_threshold_with_a_past_reset_is_probed(self):
        """m2. Mutation caught: ignoring it (the board keeps running at 99 %)."""
        r = self.started()
        self.feed(r, limit_event("allowed", T0 - 5, "five_hour", 0.99))
        self.assertIn("quota", self.active())
        self.assertIsNone(self.active()["quota"].until)

    def test_pause_notification_names_the_window(self):
        """m3 (spec §5 step 5). Mutation caught: no window type in the text."""
        r = self.started()
        self.feed(r, limit_event("rejected", T0 + HOUR, "five_hour"))
        self.feed(r, week(0.9, T0 + DAY))
        texts = [e.text for e in self.sent(r, "paused")]
        self.assertTrue(any(t.startswith("limit:") and "[five_hour]" in t for t in texts), texts)
        self.assertTrue(any(t.startswith("quota:") and "[seven_day]" in t for t in texts), texts)

    def test_wrong_shaped_stored_records_do_not_drop_events(self):
        """m4. Mutation caught: an IndexError/AttributeError in one event losing the whole tick's events."""
        self.board.set_meta("usage_samples", json.dumps([[], [1], "x", [T0 - 1, 0.1]]))
        self.board.set_meta("usage_windows", json.dumps([1, 2]))
        r = self.started()
        self.feed(r, limit_event("rejected", T0 + HOUR), week(0.9, T0 + DAY))
        self.assertEqual(self.kinds(), ["limit", "quota"])

    def test_samples_are_spaced_and_pruned(self):
        """Mutations caught: no sample gap (a sample per event); no 48 h pruning."""
        r = self.started()
        self.feed(r, week(0.10, T0 + 6 * DAY))
        self.clock.now = T0 + 5 * MIN
        self.feed(r, week(0.11, T0 + 6 * DAY))
        self.clock.now = T0 + 10 * MIN
        self.feed(r, week(0.12, T0 + 6 * DAY))
        samples = json.loads(self.board.get_meta("usage_samples"))
        self.assertEqual([s[0] for s in samples], [T0, T0 + 10 * MIN])
        self.clock.now = T0 + 49 * HOUR
        self.feed(r, week(0.2, T0 + 6 * DAY))
        self.assertEqual([s[0] for s in json.loads(self.board.get_meta("usage_samples"))], [T0 + 49 * HOUR])

    def test_limit_without_reset_uses_window_at_its_limit(self):
        """Owner A. Mutation caught: the latest reset (the weekly one) instead of the full window's."""
        r = self.started()
        self.feed(r, limit_event("allowed", T0 + 2 * HOUR, "five_hour", 0.97), week(0.4, T0 + 5 * DAY))
        self.feed(r, limit_event("rejected", None, None))
        self.assertEqual(self.active()["limit"].until, T0 + 2 * HOUR)

    def test_limit_without_reset_low_utilization_probes(self):
        """Mutation caught: taking any recorded reset; no 0.9 floor."""
        self.scenario({":probe": PROBE_429})
        r = self.started()
        self.feed(r, limit_event("allowed", T0 + 2 * HOUR, "five_hour", 0.89), week(0.4, T0 + 5 * DAY))
        self.feed(r, limit_event("rejected", None, None))
        self.assertIsNone(self.active()["limit"].until)
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)

    def test_limit_without_reset_keeps_known_end(self):
        """Owner A / M3a. Mutation caught: a reset-less hit replacing a known end."""
        r = self.started()
        self.feed(r, limit_event("rejected", T0 + 2 * HOUR), week(0.95, T0 + 5 * DAY))
        self.feed(r, limit_event("rejected", None, None))
        self.assertEqual(self.active()["limit"].until, T0 + 2 * HOUR)

    def test_limit_without_reset_past_reset_of_full_window_probes(self):
        r = self.started()
        self.feed(r, limit_event("allowed", T0 - 5, "five_hour", 0.99))
        self.feed(r, limit_event("rejected", None, None))
        self.assertIsNone(self.active()["limit"].until)

    def test_night_budget_exactly_at_the_budget_pauses(self):
        """Mutations caught: `>` instead of `>=`; float noise (0.35 - 0.20 is 0.1499...)."""
        r = self.started()
        self.feed(r, week(0.20, T0 + 6 * DAY))
        self.clock.now = T0 + 20 * MIN
        self.feed(r, week(0.35, T0 + 6 * DAY))
        self.assertIn("quota", self.active())

    def test_errored_probe_counts_against_the_cap(self):
        """Mutation caught: a probe job that raises not recorded (a missing CLI would probe for ever)."""
        r = self.started()
        r.pause("limit", "usage limit")
        self.clock.now = T0 + 30 * MIN
        with mock.patch.object(probe, "run_probe", side_effect=RuntimeError("boom")):
            self.finish_probe(r, 1)
        self.assertEqual(pauses.probe_times(self.board, now=self.clock()), [(T0 + 30 * MIN, "limit")])
        self.assertEqual(json.loads(self.board.get_meta("last_probe"))["outcome"], "unavailable")

    # controller review 2 (reviews/T35-ctl.md)

    def test_probe_warning_is_a_limit_not_ok(self):
        """Mutation caught: a probe reporting allowed_warning read as ok (both pauses lift into the warning zone)."""
        self.scenario({":probe": [{"assistant": {"input_tokens": 1, "output_tokens": 1}},
                                  {"rate_limit": ["allowed_warning", T0 + 5 * HOUR, "five_hour", 0.5]},
                                  {"result": {"summary": "OK"}}]})
        r = self.started()
        r.pause("agent_unavailable", "agent down")
        r.pause("limit", "usage limit", setting="limit_stop")
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        self.assertEqual(self.active()["limit"].until, T0 + 5 * HOUR)
        self.assertIn("agent_unavailable", self.active())
        self.assertEqual(json.loads(self.board.get_meta("last_probe"))["outcome"], "limit")

    def test_probe_warning_ignored_when_limit_stop_is_rejected(self):
        self.scenario({":probe": [{"assistant": {"input_tokens": 1, "output_tokens": 1}},
                                  {"rate_limit": ["allowed_warning", T0 + 5 * HOUR, "five_hour", 0.5]},
                                  {"result": {"summary": "OK"}}]})
        r = self.started(limit_stop="rejected")
        r.pause("agent_unavailable", "agent down")
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        self.assertEqual(self.kinds(), [])

    def test_limit_end_never_shrinks(self):
        """Mutation caught: a later report with an earlier reset shortening the known end."""
        r = self.started()
        self.feed(r, limit_event("rejected", T0 + 3 * DAY, "seven_day"))
        self.feed(r, limit_event("rejected", T0 + 4 * HOUR, "five_hour"))
        self.assertEqual(self.active()["limit"].until, T0 + 3 * DAY)
        self.feed(r, limit_event("rejected", T0 + 4 * DAY, "seven_day"))
        self.assertEqual(self.active()["limit"].until, T0 + 4 * DAY)

    def test_quota_probe_must_see_every_window_with_a_cause(self):
        """Mutation caught: a probe event without seven_day lifting a seven_day quota cause."""
        self.scenario({":probe": [{"assistant": {"input_tokens": 1, "output_tokens": 1}},
                                  {"rate_limit": ["allowed", T0 + DAY, "five_hour", 0.1]},
                                  {"result": {"summary": "OK"}}]})
        r = self.started()
        self.feed(r, week(0.9, None))
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        self.assertIn("quota", self.active())

    def test_weekly_fall_without_reset_time_is_a_roll(self):
        """Mutation caught: a negative difference silencing the night budget for 24 h."""
        r = self.started()
        self.feed(r, week(0.60, None))
        self.clock.now = T0 + 20 * MIN
        self.feed(r, week(0.05, None))                    # rolled
        self.clock.now = T0 + 40 * MIN
        self.feed(r, week(0.21, None))                    # +0.16 over the new baseline
        self.assertEqual(self.active()["quota"].causes[-1].setting, "night_budget_seven_day")

    def test_events_of_one_batch_are_all_applied(self):
        """Mutation caught: only the last queued event applied (rejected, then allowed)."""
        r = self.started()
        r.on_rate_limit(limit_event("rejected", T0 + HOUR))
        r.on_rate_limit(limit_event("allowed", T0 + HOUR, "five_hour", 0.1))
        r.tick()
        self.assertIn("limit", self.active())

    def test_probe_limit_with_a_past_reset_changes_nothing(self):
        """Mutation caught: a past reset accepted as the end."""
        self.scenario({":probe": [{"rate_limit": ["rejected", T0 - 60, "five_hour", 0.5]}, {"result_error": 429}]})
        r = self.started()
        r.pause("limit", "usage limit", setting="limit_stop")
        self.clock.now = T0 + 30 * MIN
        self.finish_probe(r, 1)
        self.assertIsNone(self.active()["limit"].until)


class CrossReview2Test(LimitCase):
    """Task 37: the T35 cross-review 2 items (reviews/T35-2.md) and their controller decisions."""

    def test_probe_does_not_end_night_budget(self):
        """m-a. Mutation caught: a probe started for a reset-less window cause lifting the whole `quota` pause, the
        night budget's cause (until baseline + 24 h) with it."""
        self.scenario({":probe": [{"assistant": {"input_tokens": 1, "output_tokens": 1}},
                                  {"rate_limit": ["allowed", T0 + DAY, "five_hour", 0.5]},
                                  {"result": {"summary": "OK"}}]})
        r = self.started()
        self.feed(r, week(0.30, T0 + 6 * DAY))
        self.clock.now = T0 + 20 * MIN
        self.feed(r, week(0.46, T0 + 6 * DAY))                       # the night budget: until T0 + 24 h
        self.feed(r, limit_event("allowed", None, "five_hour", 0.97))   # a window cause without a reset: probed
        self.assertIsNone(self.active()["quota"].until)
        self.clock.now = T0 + 50 * MIN
        self.finish_probe(r, 1)
        quota = self.active()["quota"]
        self.assertEqual([c.setting for c in quota.causes], ["night_budget_seven_day"])
        self.assertEqual(quota.until, T0 + DAY)
        self.assertNotIn("quota", [e.text for e in self.sent(r, "resumed")])
        self.clock.now = T0 + DAY
        r.tick()
        self.assertNotIn("quota", self.active())                       # it ends at its own end

    def test_past_reset_window_not_readded(self):
        """m-b. Mutation caught: a stored window whose reset has passed coming back as a probed quota cause on a
        result that did not report it (a 429-only result)."""
        r = self.started()
        self.feed(r, week(0.85, T0 + HOUR))
        self.clock.now = T0 + 2 * HOUR
        r.tick()
        self.assertEqual(self.kinds(), [])
        self.feed(r, limit_event("rejected", None, None))               # 429 only: no windows
        self.assertEqual(self.kinds(), ["limit"])
        self.assertEqual(len([e for e in self.sent(r, "paused") if e.text.startswith("quota:")]), 1)

    def test_bad_usage_windows_shape_keeps_limits(self):
        """m-c. Mutation caught: a window of the wrong inner shape raising in every event (no limit pause ever
        again), or ignored silently / journaled on every read."""
        self.board.set_meta("usage_windows", json.dumps({"seven_day": 5, "five_hour": {"utilization": "x"},
                                                         "other": {"utilization": 0.1, "resets_at": "soon"}}))
        r = self.started()
        self.feed(r, limit_event("rejected", T0 + HOUR))
        self.feed(r, week(0.9, T0 + DAY))
        self.assertEqual(self.kinds(), ["limit", "quota"])
        fails = [row["text"] for row in self.board.conn.execute("SELECT text FROM entries WHERE kind = 'FAIL'")]
        self.assertEqual(len(fails), 1, fails)
        self.assertIn("usage_windows", fails[0])
        self.assertEqual(set(json.loads(self.board.get_meta("usage_windows"))), {"seven_day"})

    def test_limit_guess_at_exactly_point_nine(self):
        """m-d N1, N2. Mutation caught: `>` for `>=` at LIMIT_WINDOW_MIN, or a floor above 0.9."""
        r = self.started()
        self.feed(r, limit_event("allowed", T0 + 2 * HOUR, "five_hour", 0.9), week(0.4, T0 + 5 * DAY))
        self.feed(r, limit_event("rejected", None, None))
        self.assertEqual(self.active()["limit"].until, T0 + 2 * HOUR)

    def test_reset_less_hit_after_known_end_expired_keeps_limit(self):
        """m-d N3. Events are applied before the pause ends are checked: a reset-less hit in the tick where a known
        end has passed must keep the limit. Mutation caught: the M3a guard without `known.until > now` (the hit is
        dropped and the limit lifted into a running limit)."""
        r = self.started()
        self.feed(r, limit_event("rejected", T0 + HOUR))
        self.clock.now = T0 + HOUR + 1
        self.feed(r, limit_event("rejected", None, None))
        self.assertIn("limit", self.active())
        self.assertIsNone(self.active()["limit"].until)
