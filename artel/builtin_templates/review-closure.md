You are doing a closure check on task #$task_id: $title

Stage: $stage, round $round. You are read-only: do not edit files.

## Open findings
$closure_list

## What to do
For each open finding above decide whether the fix closed it: look at `git diff $diff_range` and the code. Return the ids of the closed findings in `closed`, each with a basis. A finding that is not closed stays open and is returned again as a finding.

Report new findings only at `blocker` severity. Do not open new topics.

## History of this task
$task_history

## Outcome
- `pass`: every finding is closed and there is no new blocker.
- `findings`: some finding is still open or there is a new blocker.
- `ask`: only a human can tell; put the question in `question`.
