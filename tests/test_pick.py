import ast
import unittest
from pathlib import Path

from artel import pick
from tests.factories import make_task


def ids(tasks):
    return [t.id for t in tasks]


def select(tasks, deps=None, free_slots=5, verify_busy=False, busy=()):
    return ids(pick.select(tasks, deps or {}, free_slots=free_slots, verify_busy=verify_busy, busy=busy))


class AreaTest(unittest.TestCase):
    def test_normalize_area(self):
        cases = {"./src/": "src", "src//a///b/": "src/a/b", "src": "src", "": "", ".": "", "./": "", "a/./b": "a/b"}
        for raw, want in cases.items():
            self.assertEqual(pick.normalize_area(raw), want, raw)

    def test_overlap_component_prefix(self):
        self.assertFalse(pick.areas_overlap(["src/a"], ["src/ab"]))   # a string prefix is not a path prefix
        self.assertTrue(pick.areas_overlap(["src"], ["src/a"]))
        self.assertTrue(pick.areas_overlap(["src/a/x.py"], ["src/a"]))
        self.assertTrue(pick.areas_overlap(["./src/"], ["src/a"]))
        self.assertTrue(pick.areas_overlap(["src/a"], ["src/a"]))
        self.assertFalse(pick.areas_overlap(["docs"], ["src", "tests"]))
        self.assertTrue(pick.areas_overlap(["docs", "src/a"], ["src"]))
        self.assertTrue(pick.areas_overlap([], ["anything"]))
        self.assertTrue(pick.areas_overlap(["anything"], []))
        self.assertTrue(pick.areas_overlap([], []))
        self.assertTrue(pick.areas_overlap(["."], ["src"]))   # "." is the whole repository

    def test_holds_area(self):
        for state in pick.IN_FLIGHT:
            self.assertTrue(pick.holds_area(make_task(state=state)), state)
        for state in ("proposed", "vetting", "awaiting_approval", "open", "blocked", "awaiting_accept",
                      "done", "rejected", "stuck"):
            self.assertFalse(pick.holds_area(make_task(state=state)), state)
        self.assertFalse(pick.holds_area(make_task(state="attacking", then="open")))
        self.assertTrue(pick.holds_area(make_task(state="attacking", then=None)))


class SelectTest(unittest.TestCase):
    def test_answered_first(self):
        tasks = [make_task(id=1, state="writing", priority=9, area=("a",)),
                 make_task(id=2, state="fixing", answered=True, area=("b",)),
                 make_task(id=3, state="reviewing", area=("c",))]
        self.assertEqual(select(tasks), [2, 1, 3])

    def test_in_flight_stages_before_open(self):
        tasks = [make_task(id=1, state="open", priority=5, area=("x",)),
                 make_task(id=2, state="writing", area=("y",))]
        self.assertEqual(select(tasks), [2, 1])

    def test_priority_then_id(self):
        tasks = [make_task(id=1, state="open", priority=0, area=("a",)),
                 make_task(id=2, state="open", priority=3, area=("b",)),
                 make_task(id=3, state="open", priority=3, area=("c",)),
                 make_task(id=4, state="open", priority=-1, area=("d",))]
        self.assertEqual(select(tasks), [2, 3, 1, 4])

    def test_running_tasks_never_selected(self):
        tasks = [make_task(id=1, state="writing", running=True, area=("a",)),
                 make_task(id=2, state="reviewing", area=("b",))]
        self.assertEqual(select(tasks), [2])
        self.assertEqual(select([make_task(id=1, state="writing", area=("a",))], busy=[1]), [])

    def test_verifying_only_one_and_not_when_busy(self):
        tasks = [make_task(id=5, state="verifying", area=("a",)), make_task(id=3, state="verifying", area=("b",))]
        self.assertEqual(select(tasks), [3])
        self.assertEqual(select(tasks, verify_busy=True), [])

    def test_open_task_with_overlapping_area_waits(self):
        running = make_task(id=1, state="reviewing", area=("src/a",), running=True)
        blocked = make_task(id=2, state="open", area=("src/a/x.py",))
        free = make_task(id=3, state="open", area=("src/ab",))
        got = pick.select([running, blocked, free], {}, free_slots=2, verify_busy=False)
        self.assertEqual([t.id for t in got], [3])

    def test_two_open_overlapping_tasks_not_selected_together(self):
        tasks = [make_task(id=1, state="open", area=("src",)), make_task(id=2, state="open", area=("src/a",)),
                 make_task(id=3, state="open", area=("docs",))]
        self.assertEqual(select(tasks), [1, 3])

    def test_empty_area_open_task_blocks_and_is_blocked(self):
        tasks = [make_task(id=1, state="open", area=()), make_task(id=2, state="open", area=("a",))]
        self.assertEqual(select(tasks), [1])
        tasks = [make_task(id=1, state="writing", area=("a",), running=True), make_task(id=2, state="open", area=())]
        self.assertEqual(select(tasks), [])

    def test_after_dependency_must_be_done(self):
        tasks = [make_task(id=1, state="open", area=("a",)), make_task(id=2, state="done"),
                 make_task(id=3, state="open", area=("b",)), make_task(id=4, state="open", area=("c",))]
        deps = {3: [2], 4: [1], 1: [99]}
        self.assertEqual(select(tasks, deps), [3])   # 4 waits for the open #1, 1 waits for a missing task

    def test_free_slots_limit(self):
        tasks = [make_task(id=i, state="open", area=(f"d{i}",)) for i in range(1, 6)]
        self.assertEqual(select(tasks, free_slots=2), [1, 2])
        self.assertEqual(select(tasks, free_slots=0), [])

    def test_proposed_picked_for_vetting_before_open(self):
        tasks = [make_task(id=1, state="open", area=("a",)), make_task(id=2, state="proposed", priority=1),
                 make_task(id=3, state="proposed")]
        self.assertEqual(select(tasks), [2, 3, 1])
        self.assertEqual(select(tasks, free_slots=1), [2])

    def test_proposed_ignores_areas(self):
        tasks = [make_task(id=1, state="writing", area=("a",)), make_task(id=2, state="proposed", area=("a",))]
        self.assertEqual(select(tasks), [1, 2])

    def test_vetting_is_a_due_stage(self):
        tasks = [make_task(id=1, state="vetting", area=("a",)), make_task(id=2, state="open", area=("b",))]
        self.assertEqual(select(tasks), [1, 2])

    def test_branchless_attack_holds_no_area(self):
        tasks = [make_task(id=1, state="attacking", then="awaiting_approval", area=("src",), running=True),
                 make_task(id=2, state="open", area=("src/a",))]
        self.assertEqual(select(tasks), [2])

    def test_human_attack_holds_area(self):
        tasks = [make_task(id=1, state="attacking", then=None, area=("src",), running=True),
                 make_task(id=2, state="open", area=("src/a",))]
        self.assertEqual(select(tasks), [])

    def test_busy_task_not_selected_but_holds_area(self):
        # catches: dispatching one task twice, or starting an overlapping task while the first one boots
        tasks = [make_task(id=1, state="open", area=("src",)), make_task(id=2, state="open", area=("src/a",)),
                 make_task(id=3, state="open", area=("docs",))]
        self.assertEqual(select(tasks, busy=[1]), [3])

    def test_answered_task_waits_for_overlapping_running_task(self):
        answered = make_task(id=1, state="writing", answered=True, area=("src",))
        running = make_task(id=2, state="reviewing", area=("src/a",), running=True)
        self.assertEqual(select([answered, running]), [])
        self.assertEqual(select([answered, make_task(id=2, state="reviewing", area=("src/a",))]), [1])
        self.assertEqual(select([answered, make_task(id=2, state="reviewing", area=("docs",), running=True)]), [1])

    def test_two_due_overlapping_tasks_not_selected_together(self):
        tasks = [make_task(id=1, state="writing", area=("src",)), make_task(id=2, state="fixing", area=("src/a",)),
                 make_task(id=3, state="reviewing", area=("docs",))]
        self.assertEqual(select(tasks), [1, 3])

    def test_vetting_runs_next_to_overlapping_running_task(self):
        tasks = [make_task(id=1, state="writing", area=("src",), running=True),
                 make_task(id=2, state="vetting", area=("src",))]
        self.assertEqual(select(tasks), [2])

    def test_selected_vetting_does_not_block_overlapping_task(self):
        tasks = [make_task(id=1, state="vetting", area=("src",)), make_task(id=2, state="writing", area=("src",))]
        self.assertEqual(select(tasks), [1, 2])

    def test_verifying_blocked_by_overlapping_running_task(self):
        tasks = [make_task(id=1, state="writing", area=("src",), running=True),
                 make_task(id=2, state="verifying", area=("src/a",)),
                 make_task(id=3, state="verifying", area=("docs",))]
        self.assertEqual(select(tasks), [3])

    def test_due_branchless_attack_runs_next_to_overlapping_running_task(self):
        # catches: treating a vet-triggered attack (then set) as an area holder in steps 1-3
        tasks = [make_task(id=1, state="writing", area=("src",), running=True),
                 make_task(id=2, state="attacking", then="open", area=("src",))]
        self.assertEqual(select(tasks), [2])

    def test_only_done_dependencies_unblock(self):
        # catches: accepting verified / awaiting_accept / rejected as "done"
        for state in ("verified", "awaiting_accept", "rejected", "stuck", "blocked"):
            tasks = [make_task(id=1, state=state), make_task(id=2, state="open", area=("a",))]
            self.assertEqual(select(tasks, {2: [1]}), [], state)
        tasks = [make_task(id=1, state="done"), make_task(id=2, state="open", area=("a",))]
        self.assertEqual(select(tasks, {2: [1]}), [2])

    def test_verifying_picks_lowest_id_not_priority(self):
        tasks = [make_task(id=5, state="verifying", priority=9, area=("a",)),
                 make_task(id=3, state="verifying", priority=0, area=("b",))]
        self.assertEqual(select(tasks), [3])

    def test_blocked_and_stuck_tasks_are_not_selected(self):
        tasks = [make_task(id=i, state=s) for i, s in enumerate(
            ("blocked", "stuck", "awaiting_approval", "awaiting_accept", "done", "rejected", "verified"), 1)]
        self.assertEqual(select(tasks), [])


class WaitingReasonTest(unittest.TestCase):
    def test_waiting_reason_strings(self):
        tasks = {1: make_task(id=1, state="open"), 2: make_task(id=2, state="writing", area=("src",)),
                 3: make_task(id=3, state="open", area=("src/a",)), 4: make_task(id=4, state="rejected"),
                 5: make_task(id=5, state="open", area=("docs",))}
        self.assertEqual(pick.waiting_reason(tasks[3], tasks, [1]), "after #1 (open)")
        self.assertEqual(pick.waiting_reason(tasks[3], tasks, [4]), "after #4 (rejected)")
        self.assertEqual(pick.waiting_reason(tasks[3], tasks, [99]), "after #99 (missing)")
        self.assertEqual(pick.waiting_reason(tasks[3], tasks, []), "area overlaps #2")
        self.assertIsNone(pick.waiting_reason(tasks[5], tasks, []))
        self.assertIsNone(pick.waiting_reason(tasks[2], tasks, []))   # not an open task
        tasks[2] = make_task(id=2, state="verified", area=("src",))   # verified still holds its area
        self.assertEqual(pick.waiting_reason(tasks[3], tasks, []), "area overlaps #2")

    def test_dependency_reason_wins_over_area(self):
        tasks = {1: make_task(id=1, state="open"), 2: make_task(id=2, state="writing", area=("src",)),
                 3: make_task(id=3, state="open", area=("src",))}
        self.assertEqual(pick.waiting_reason(tasks[3], tasks, [1]), "after #1 (open)")


class PurityTest(unittest.TestCase):
    def test_pick_imports_no_board_git_or_io_modules(self):
        source = (Path(__file__).resolve().parents[1] / "artel" / "pick.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        runtime = []
        for node in tree.body:   # module level only: TYPE_CHECKING imports sit inside an `if`
            if isinstance(node, ast.Import):
                runtime += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                runtime.append(node.module or "")
        self.assertEqual(sorted(runtime), ["__future__", "collections.abc", "typing"])
