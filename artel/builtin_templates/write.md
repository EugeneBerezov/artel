Implement task #$task_id: $title

Stage: $stage, branch: $branch. Work only in the current directory.

## The task
$body

Area: $area

## Notes from the attack on this task
$findings

## History of this task
$task_history

$required_tests## Answer from the human (if any)
$answer

## Context from the journal
$context

## How to work
- Write tests first where the project has tests; run the checks for what you touched.
- Commit your work on the current branch. Report `ready` only after committing; uncommitted work does not count. Only add commits: never amend, rebase or reset the branch, and never touch another branch, a stash or the git configuration.
- If you are blocked and only a human can decide, return `ask` with the question. If you cannot do it, return `fail` with the reason.
