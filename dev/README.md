# dev/board.py - task board for parallel peer sessions

Dev tool (not part of the `artel` package). Python >= 3.11 (`python3`), stdlib only, Linux (`fcntl.flock`).
State lives in `<git-common-dir>/artel-dev/` (`board.json`, `journal.md`, `cursors/<session>`, lock files), shared by all worktrees. Every mutation takes an exclusive flock. Global option: `--repo DIR` (default `.`).

    python3 dev/board.py init --from docs/plan.md [--force]   # parse tasks; --force moves board, journal and cursors to artel-dev/backup-<time>/ and starts clean
    python3 dev/board.py claim --session S                    # next task (lowest wave, then id, deps done); exit 3 if none
    python3 dev/board.py release ID [--reason R]              # back to open
    python3 dev/board.py fail ID --reason R                   # back to open; stuck after 2 fails
    python3 dev/board.py note FACT|FAIL|NOTE|SPEC-GAP ID TEXT [--basis B] [--session S]   # text <= 400 B, basis <= 200 B
    python3 dev/board.py tail --session S [--all]             # journal entries since your cursor
    python3 dev/board.py status                               # table + counts
    python3 dev/board.py integrate ID --branch B --session S  # ID must be claimed by S; fetch, merge --no-ff into main, run tests/, push; rolls back on failure, Ctrl-C or SIGTERM
    python3 dev/board.py ci [--sha SHA]                       # all runs of the sha on main: 0 all ok, 1 any failed, 2 no run yet, 4 pending, 5 error
    python3 dev/board.py done ID [--sha S]                    # mark done by hand (controller tasks: 16, 25)

Plan format: `### Task N: title` headings (`**RISKY ...**` in the title or bold in the wave table = risky) plus the `| Wn | ... |` wave table. Deps: every task of the previous wave, plus `a -> b` chains in a wave cell; a `**Depends on:** Task 3, 4` line in a task section overrides. "controller task" titles are never claimed by peers.
`integrate` works in the main worktree (first in `git worktree list`): on `main`, no tracked changes (untracked ignored), equal to `origin/main` after `git fetch` (else it refuses and prints the fix). Integrations hold their own lock, so `claim`/`note` stay responsive while tests run (timeout 20 min, whole process group killed). Network git calls time out after 120 s.
`ci` reads the token from `~/.config/artel-dev/github-token`. Tests: `python3 -m unittest discover -s dev -q`
