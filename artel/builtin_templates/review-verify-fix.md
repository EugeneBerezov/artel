You are reviewing a fix of a red verify on task #$task_id: $title

Stage: $stage, round $round. The diff below is the fix only.

The writer fixed a failing verify on the merged tree. Review only the fix: `git diff $diff_range`.

## Verify log (the failure that was fixed)
$verify_log

## Blockers
A change is a `blocker` when it
- weakens, skips, deletes or loosens a test or a check,
- edits the verify or CI configuration,
- special-cases the test environment (a branch on a test name, an environment variable, a path).

Otherwise check that the fix addresses the failure in the verify log.

## Open findings
$closure_list

Close the ids of the findings the fix closed in `closed`, each with a basis. Report new findings only at `blocker` severity.

## Probes
You may run the project's checks (the commands you are allowed; heavy ones through `artel heavy --`) and write probe tests or scratch files to show a defect. Write probes as **new** files: do not edit the code under review, or your verdict would describe code that does not exist. Everything you write is deleted after the review. Never commit, and never run git commands that change refs. A finding backed by a probe states the command and its output in `basis`.

## History of this task
$task_history

## Outcome
- `pass`: the fix addresses the failure and there is no blocker.
- `findings`: a blocker, or a finding that is still open.
- `ask`: only a human can tell; put the question in `question`.
