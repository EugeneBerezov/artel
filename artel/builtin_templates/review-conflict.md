You are reviewing the resolution of a merge conflict on task #$task_id: $title

Stage: $stage, round $round. The commits below are the fix only.

The writer merged `artel/integration` into the task branch and resolved conflicts. Review only the resolution: list the merges with `git log --merges $diff_range`, then run `git show --remerge-diff <sha>` for each merge.

## Blockers
A resolution that drops a change that came from `artel/integration` (the work of a task that is already merged) is a `blocker`. So is a resolution that silently keeps only one side where both were needed.

## Open findings
$closure_list

Close the ids of the findings the resolution closed in `closed`, each with a basis. Report new findings only at `blocker` severity.

## Probes
You may run the project's checks (the commands you are allowed; heavy ones through `artel heavy --`) and write probe tests or scratch files to show a defect. Write probes as **new** files: do not edit the code under review, or your verdict would describe code that does not exist. Everything you write is deleted after the review. Never commit, and never run git commands that change refs. A finding backed by a probe states the command and its output in `basis`.

## History of this task
$task_history

## Outcome
- `pass`: the resolution keeps both sides' changes and there is no blocker.
- `findings`: a blocker, or a finding that is still open.
- `ask`: only a human can tell; put the question in `question`.
