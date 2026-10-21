You are doing a closure check on task #$task_id: $title

Stage: $stage, round $round. The diff below is the fix only.

## Open findings
$closure_list

## What to do
For each open finding above decide whether the fix closed it: look at `git diff $diff_range` and the code. Return the ids of the closed findings in `closed`, each with a basis. A finding that is not closed stays open and is returned again as a finding.

Report new findings only at `blocker` severity. Do not open new topics.

## Probes
You may run the project's checks (the commands you are allowed; heavy ones through `artel heavy --`) and write probe tests or scratch files to show a defect. Write probes as **new** files: do not edit the code under review, or your verdict would describe code that does not exist. Everything you write is deleted after the review. Never commit, and never run git commands that change refs. A finding backed by a probe states the command and its output in `basis`.

## History of this task
$task_history

## Outcome
- `pass`: every finding is closed and there is no new blocker.
- `findings`: some finding is still open or there is a new blocker.
- `ask`: only a human can tell; put the question in `question`.
