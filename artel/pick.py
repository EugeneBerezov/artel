"""Pick order and area overlap (spec §3, §5 step 2; PD-6, PD-7). Pure: no board, git or I/O imports."""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # annotation only: pick imports no board, git or I/O module at run time
    from artel.board import Task

IN_FLIGHT = ("attacking", "writing", "reviewing", "fixing", "verifying", "verified")
DUE_STAGES = ("vetting", "attacking", "writing", "reviewing", "fixing")


def normalize_area(path: str) -> str:
    return "/".join(p for p in path.split("/") if p not in ("", "."))


def _normalized(area: Sequence[str]) -> list[str]:
    return [normalize_area(p) for p in area]


def areas_overlap(a: Sequence[str], b: Sequence[str]) -> bool:
    """An empty area (or a path that normalizes to "") is the whole repository and overlaps everything."""
    na, nb = _normalized(a), _normalized(b)
    if not na or not nb or "" in na or "" in nb:
        return True
    return any(x == y or x.startswith(y + "/") or y.startswith(x + "/") for x in na for y in nb)


def holds_area(task: Task) -> bool:
    return task.state in IN_FLIGHT and not (task.state == "attacking" and task.then is not None)


def _order(tasks):
    return sorted(tasks, key=lambda t: (-t.priority, t.id))


def select(tasks: Sequence[Task], deps: Mapping[int, Sequence[int]], *, free_slots: int,
           verify_busy: bool, busy: Collection[int] = ()) -> list[Task]:
    by_id = {t.id: t for t in tasks}
    running = set(busy) | {t.id for t in tasks if t.running}
    # a running task holds its area; an `open` task picked but not yet started will hold it too
    running_holders = [t for t in tasks if t.id in running and (holds_area(t) or t.state == "open")]
    holders = [t for t in tasks if holds_area(t)] + [t for t in running_holders if not holds_area(t)]
    picked: list[Task] = []
    picked_areas: list[Sequence[str]] = []

    def full() -> bool:
        return len(picked) >= free_slots

    def clear_of_running(t: Task) -> bool:
        if t.state == "vetting" or not holds_area(t):
            return True              # holds no area: vet, vet-triggered attack
        return not any(areas_overlap(t.area, h.area) for h in running_holders if h.id != t.id) \
            and not any(areas_overlap(t.area, a) for a in picked_areas)

    def take(t: Task) -> None:
        picked.append(t)
        if holds_area(t) or t.state == "open":
            picked_areas.append(t.area)

    candidates = [t for t in tasks if t.id not in running]
    due = [t for t in candidates if t.state in DUE_STAGES]
    for group in (_order(t for t in due if t.answered), _order(t for t in due if not t.answered)):
        for t in group:
            if not full() and t not in picked and clear_of_running(t):
                take(t)
    if not verify_busy and not full():
        for t in sorted((t for t in candidates if t.state == "verifying"), key=lambda t: t.id):
            if clear_of_running(t):
                take(t)
                break
    for t in _order(t for t in candidates if t.state == "proposed"):
        if full():
            break
        picked.append(t)             # the runner turns proposed → vetting; no area check
    for t in _order(t for t in candidates if t.state == "open"):
        if full():
            break
        if not all(d in by_id and by_id[d].state == "done" for d in deps.get(t.id, ())):
            continue
        if any(areas_overlap(t.area, h.area) for h in holders if h.id != t.id):
            continue
        if any(areas_overlap(t.area, a) for a in picked_areas):
            continue
        take(t)
    return picked


def waiting_reason(task: Task, tasks_by_id: Mapping[int, Task], deps: Sequence[int]) -> str | None:
    if task.state != "open":
        return None
    for dep_id in deps:
        dep = tasks_by_id.get(dep_id)
        if dep is None or dep.state != "done":
            return f"after #{dep_id} ({dep.state if dep else 'missing'})"
    for other in sorted(tasks_by_id.values(), key=lambda t: t.id):
        if other.id != task.id and holds_area(other) and areas_overlap(task.area, other.area):
            return f"area overlaps #{other.id}"
    return None
