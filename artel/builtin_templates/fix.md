Fix task #$task_id: $title

Stage: $stage, branch: $branch. Work only in the current directory.

## The task
$body

## Open findings to fix
$findings

## Verify log (when the merged tree failed verification)
$verify_log

## Merge in progress (when integration conflicted)
If conflicts are listed, the runner started a merge in this directory and these paths conflict: $conflict_paths
Resolve the conflict markers, `git add` the files and `git commit` to conclude the merge. You never start or abort a merge yourself.

## History of this task
$task_history

## Answer from the human (if any)
$answer

## Context from the journal
$context

## How to work
Fix what is listed, run the checks for what you touched, commit on the current branch (only add commits: never amend, rebase or reset it, never touch another branch, a stash or the git configuration) and return `ready`. If only a human can decide, return `ask`.
