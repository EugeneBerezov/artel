from artel import board


def make_task(**overrides) -> board.Task:
    """A Task with every field set to a neutral default, for pure tests without a database."""
    fields = dict(
        id=1, title="task", body="", area=(), priority=0, state="open", created_by="human",
        attack=False, strong=False, review_rounds=None, merge=None,
        extra_review_rounds=0, extra_verify_fixes=0, attempts=0, round=0, verify_fixes=0,
        fix_kind=None, asked_by=None, cause=None, then=None, rejected_by=None, stuck_from=None,
        review_closure=False, answered=False, last_verify=None,
        running=False, running_kind=None, running_started_at=None, running_pid=None,
        running_group=None, running_fingerprint=None, running_slot=None,
        version=0, created_at=0.0, updated_at=0.0,
    )
    fields.update(overrides)
    return board.Task(**fields)
