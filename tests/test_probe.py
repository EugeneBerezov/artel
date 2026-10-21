"""Task 35: the limit probe (PD-55) — argv, the call, the schedule and when it ends a pause."""
import threading
import re
import time

from artel import config as C, pauses, probe
from artel.pauses import Cause, Pause
from tests.e2e import E2ECase

HOUR = 3600.0
MIN = 60.0
LIMIT_RESET = 1_800_000_000
UNAVAILABLE = [{"exit": 1}]
LIMIT = [{"rate_limit": ["rejected", LIMIT_RESET, "five_hour", 0.99]}, {"result_error": 429}]
LIMIT_NO_TIME = [{"result_error": 429}]


def window_ok(utilization, resets=LIMIT_RESET, name="seven_day"):
    return [{"assistant": {"input_tokens": 1, "output_tokens": 1}}, {"rate_limit": ["allowed", resets, name, utilization]},
            {"result": {"summary": "OK"}}]


def pause_of(kind="limit", since=0.0, until=None, exhausted=False):
    return Pause(kind, since, (Cause("x", None, until),), exhausted)


class ProbeCase(E2ECase):
    def setUp(self):
        super().setUp()
        self.write_toml({"quota_stop_five_hour": 0.95, "quota_stop_seven_day": 0.8, "night_budget_seven_day": 0.15,
                         "limit_retry": "30m", "limit_probes": 4})
        self.cfg = C.load_config(self.repo, env={})

    def run_probe(self, now=1_700_000_000.0, env=None):
        return probe.run_probe(self.repo, self.cfg, dict(self.env if env is None else env), now=now)


class ProbeArgvTest(ProbeCase):
    def test_probe_argv_has_no_tools_no_schema(self):
        """Mutation caught: the reviewer/writer profile or profiles.common (schema, notice, tools) reaching the probe."""
        argv = probe.probe_argv(self.cfg)
        text = " ".join(argv)
        self.assertNotIn("--json-schema", argv)
        self.assertNotIn("--allowedTools", argv)
        self.assertNotIn("--append-system-prompt-file", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(argv[argv.index("--tools") + 1], "")          # no tools at all (Task 44)
        self.assertIn("--max-turns 1", text)
        self.assertEqual(argv.count("--setting-sources"), 1)            # the pair of profiles.common (PD-55)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")

    def test_probe_setting_sources_not_duplicated(self):
        """Mutation caught: the common pair appended when the probe profile sets its own."""
        profile = list(self.cfg["profiles.probe"]) + ["--setting-sources", "user"]
        self.write_toml({"profiles.probe": profile})
        argv = probe.probe_argv(C.load_config(self.repo, env={}))
        self.assertEqual(argv.count("--setting-sources"), 1)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")

    def test_probe_setting_sources_equals_form(self):
        profile = list(self.cfg["profiles.probe"]) + ["--setting-sources=user"]
        self.write_toml({"profiles.probe": profile})
        argv = probe.probe_argv(C.load_config(self.repo, env={}))
        self.assertEqual([t for t in argv if t.startswith("--setting-sources")], ["--setting-sources=user"])

    def test_probe_without_common_setting_sources_adds_nothing(self):
        common = [t for t in self.cfg["profiles.common"]]
        i = common.index("--setting-sources")
        self.write_toml({"profiles.common": common[:i] + common[i + 2:]})
        argv = probe.probe_argv(C.load_config(self.repo, env={}))
        self.assertNotIn("--setting-sources", argv)


class RunProbeTest(ProbeCase):
    def test_probe_log_name_has_no_colon(self):
        """Mutation caught: an ISO timestamp in the log name (invalid on Windows); two probes in one instant
        overwriting each other's log."""
        first, second = self.run_probe(), self.run_probe()
        for result in (first, second):
            self.assertRegex(result.log_path.name, r"^\d{8}T\d{12}Z(-\d+)?\.jsonl$")
            self.assertNotIn(":", result.log_path.name)
            self.assertEqual(result.log_path.parent, self.repo.logs_dir / "probe")
        self.assertNotEqual(first.log_path, second.log_path)
        self.assertTrue(first.log_path.is_file())

    def test_probe_runs_in_an_empty_directory_without_a_task(self):
        """Mutation caught: the probe started in a slot or with ARTEL_TASK set."""
        self.run_probe()
        (call,) = self.calls()
        self.assertEqual((call["task"], call["stage"]), ("", "probe"))
        self.assertEqual(call["cwd"].replace("\\", "/").rsplit("/", 2)[-2:], ["artel", "probe"])
        self.assertEqual(call["prompt"], probe.PROMPT)

    def test_probe_outcomes(self):
        """Mutation caught: a limit read as ok, a crash read as ok, the reset time or windows dropped."""
        cases = [
            ("ok", None, None, {}),
            ("limit", LIMIT, LIMIT_RESET, {"five_hour": {"utilization": 0.99, "resets_at": float(LIMIT_RESET)}}),
            ("limit", LIMIT_NO_TIME, None, {}),
            ("unavailable", UNAVAILABLE, None, {}),
            ("unavailable", [{"result_error": 503}], None, {}),
            ("ok", window_ok(0.5), None, {"seven_day": {"utilization": 0.5, "resets_at": float(LIMIT_RESET)}}),
        ]
        for outcome, actions, resets, windows in cases:
            with self.subTest(outcome=outcome, actions=actions):
                (self.fake_dir / "calls.jsonl").unlink(missing_ok=True)
                (self.fake_dir / "counts.json").unlink(missing_ok=True)
                self.scenario({":probe": actions} if actions is not None else {})
                result = self.run_probe()
                self.assertEqual((result.outcome, result.resets_at, result.windows), (outcome, resets, windows))

    def test_probe_stop_event_ends_it(self):
        """Mutation caught: a probe that ignores the stop event (a stop request would wait 120 s)."""
        self.scenario({":probe": [{"sleep": 30}]})
        stop = threading.Event()
        threading.Timer(1.0, stop.set).start()
        started = time.monotonic()
        result = probe.run_probe(self.repo, self.cfg, dict(self.env), now=1.0, stop_event=stop)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.outcome, "unavailable")


    def test_probe_timeout_is_unavailable_even_after_an_assistant_record(self):
        """Mutation caught: a timed-out probe read as ok (it would lift the pause)."""
        from unittest import mock
        self.scenario({":probe": [{"assistant": {"input_tokens": 1, "output_tokens": 1}}, {"sleep": 30}]})
        with mock.patch.object(probe, "PROBE_TIMEOUT", 1):
            self.assertEqual(self.run_probe().outcome, "unavailable")


class DueTest(ProbeCase):
    def test_first_probe_is_due_one_retry_after_since(self):
        """Mutation caught: k = 0 (a probe at once), or measuring from the wrong anchor."""
        pause = pause_of(since=100.0)
        self.assertFalse(probe.due(pause, self.cfg, now=100.0 + 30 * MIN - 1, times=[]))
        self.assertTrue(probe.due(pause, self.cfg, now=100.0 + 30 * MIN, times=[]))

    def test_gaps_grow_by_one_retry_each_from_the_previous_probe(self):
        """The plan's schedule: probes at +30, +90, +180, +300 min (gaps 30, 60, 90, 120)."""
        pause = pause_of(since=0.0)
        times = []
        for k, at in enumerate((30, 90, 180, 300), start=1):
            self.assertFalse(probe.due(pause, self.cfg, now=at * MIN - 1, times=times), at)
            self.assertTrue(probe.due(pause, self.cfg, now=at * MIN, times=times), at)
            times = times + [(at * MIN, "limit")]

    def test_clock_jump_runs_one_probe_and_the_next_waits_k_plus_one_retries(self):
        """Mutation caught: anchoring at `since` (a probe burst after a long sleep)."""
        pause = pause_of(since=0.0)
        self.assertTrue(probe.due(pause, self.cfg, now=8 * HOUR, times=[]))
        times = [(8 * HOUR, "limit")]
        self.assertFalse(probe.due(pause, self.cfg, now=8 * HOUR + 60 * MIN - 1, times=times))
        self.assertTrue(probe.due(pause, self.cfg, now=8 * HOUR + 60 * MIN, times=times))

    def test_cap_counts_every_kind_within_24_hours(self):
        """Mutation caught: a per-kind cap, or counting probes older than 24 h."""
        pause = pause_of(since=0.0)
        now = 30 * HOUR
        mixed = [(now - 3 * HOUR, "agent_unavailable"), (now - 2 * HOUR, "quota"), (now - 90 * MIN, "limit"),
                 (now - 80 * MIN, "agent_unavailable")]
        self.assertFalse(probe.due(pause, self.cfg, now=now, times=mixed))
        old = [(now - 25 * HOUR, "limit")] + mixed[1:]
        self.assertTrue(probe.due(pause, self.cfg, now=now, times=old))

    def test_zero_probes_never_probes(self):
        self.write_toml({"limit_probes": 0})
        self.assertFalse(probe.due(pause_of(), C.load_config(self.repo, env={}), now=99 * HOUR, times=[]))

    def test_due_skips_kinds_outside_probed_kinds(self):
        """Mutation caught: probing a `manual` or `disk` pause whose `until` is None, a pause with a known end, or
        an exhausted one."""
        for kind in ("manual", "disk", "failures", "base_conflict", "setup_failed"):
            with self.subTest(kind=kind):
                self.assertFalse(probe.due(pause_of(kind), self.cfg, now=99 * HOUR, times=[]))
        self.assertFalse(probe.due(pause_of(until=500.0), self.cfg, now=99 * HOUR, times=[]))
        self.assertFalse(probe.due(pause_of(exhausted=True), self.cfg, now=99 * HOUR, times=[]))
        for kind in ("limit", "agent_unavailable", "quota"):
            self.assertTrue(probe.due(pause_of(kind), self.cfg, now=99 * HOUR, times=[]), kind)

    def test_two_kinds_keep_their_own_schedule(self):
        """Mutation caught: one shared `last_probe` (a probe of one kind pushing the other's)."""
        limit, unavailable = pause_of("limit", since=0.0), pause_of("agent_unavailable", since=10 * MIN)
        times = [(30 * MIN, "limit"), (45 * MIN, "agent_unavailable")]
        # limit: second probe 60 min after its own last probe (30 min) = 90 min
        self.assertFalse(probe.due(limit, self.cfg, now=89 * MIN, times=times))
        self.assertTrue(probe.due(limit, self.cfg, now=90 * MIN, times=times))
        # agent_unavailable: second probe 60 min after its own (45 min) = 105 min
        self.assertFalse(probe.due(unavailable, self.cfg, now=104 * MIN, times=times))
        self.assertTrue(probe.due(unavailable, self.cfg, now=105 * MIN, times=times))

    def test_readded_pause_restarts_k_but_the_cap_still_counts(self):
        """Mutation caught: counting the probes of an earlier pause of the kind toward k (probes before `since`)."""
        now = 10 * HOUR
        times = [(1 * HOUR, "limit"), (2 * HOUR, "limit")]
        readded = pause_of("limit", since=9 * HOUR)
        self.assertFalse(probe.due(readded, self.cfg, now=9 * HOUR + 30 * MIN - 1, times=times))
        self.assertTrue(probe.due(readded, self.cfg, now=9 * HOUR + 30 * MIN, times=times))   # k = 1 again
        full = times + [(3 * HOUR, "quota"), (4 * HOUR, "quota")]
        self.assertFalse(probe.due(readded, self.cfg, now=now, times=full))                  # four within 24 h


class EndedTest(ProbeCase):
    def result(self, outcome, windows=None):
        return probe.ProbeResult(outcome, None, self.repo.logs_dir / "x", windows or {})

    def test_ok_ends_every_kind_but_quota(self):
        for kind in ("limit", "agent_unavailable"):
            self.assertTrue(probe.ended(pause_of(kind), self.result("ok"), self.cfg, now=1.0), kind)

    def test_limit_and_unavailable_never_end_a_pause(self):
        for outcome in ("limit", "unavailable"):
            for kind in ("limit", "agent_unavailable", "quota"):
                self.assertFalse(probe.ended(pause_of(kind), self.result(outcome), self.cfg, now=1.0))

    def test_quota_ends_only_with_every_window_below_its_threshold(self):
        """Mutation caught: ending quota on any `ok`, comparing with the wrong threshold, ignoring a window, or
        ending it when the probe saw no window at all."""
        ok = lambda five, seven: self.result("ok", {"five_hour": {"utilization": five, "resets_at": 5.0},
                                                    "seven_day": {"utilization": seven, "resets_at": 5.0}})
        quota = pause_of("quota")
        self.assertTrue(probe.ended(quota, ok(0.5, 0.5), self.cfg, now=1.0))
        self.assertFalse(probe.ended(quota, ok(0.96, 0.5), self.cfg, now=1.0))     # five_hour >= 0.95
        self.assertFalse(probe.ended(quota, ok(0.5, 0.85), self.cfg, now=1.0))     # seven_day >= 0.8
        self.assertTrue(probe.ended(quota, ok(0.9, 0.5), self.cfg, now=1.0))       # 0.9 is below five_hour's 0.95
        self.assertFalse(probe.ended(quota, self.result("ok"), self.cfg, now=1.0))

    def test_event_window_keeps_its_own_reset_when_the_event_has_none(self):
        """Review finding. Mutation caught: overwriting the window's reset with the event's None."""
        from artel.agent_claude import RateLimit
        ev = RateLimit("allowed", 0, None, "five_hour", 0.5, {"five_hour": {"utilization": 0.5, "resetsAt": 99}})
        self.assertEqual(probe.event_windows(ev), {"five_hour": {"utilization": 0.5, "resets_at": 99.0}})

    def test_quota_with_no_known_window_does_not_end(self):
        ok = probe.ProbeResult("ok", None, self.repo.logs_dir / "x", {"other": {"utilization": 0.1, "resets_at": 1.0}})
        self.assertFalse(probe.ended(pause_of("quota"), ok, self.cfg, now=1.0))
