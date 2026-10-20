"""Notification events (spec §7). Texts never carry secrets or file contents."""
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass

EVENT_KINDS = ("blocked", "stuck", "paused", "resumed", "done", "runner_stopped", "error")   # error: PD-38


@dataclass(frozen=True)
class Event:
    kind: str
    task_id: int | None = None
    title: str | None = None
    state: str | None = None
    text: str | None = None
    resets_at: float | None = None

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in asdict(self).items() if v is not None}, ensure_ascii=False)


Notify = Callable[[Event], None]


def null_notify(event: Event) -> None:
    return None
