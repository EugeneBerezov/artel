# dev/board.py - task board for parallel peer sessions

Dev tool (not part of the `artel` package). Python >= 3.11, stdlib only, Linux (`fcntl.flock`).
State lives in `<git-common-dir>/artel-dev/` (`board.json`, `journal.md`, `cursors/<session>`, lock files), shared by all worktrees. Every mutation takes an exclusive flock. Global option: `--repo DIR` (default `.`).

    python dev/board.py init --from docs/plan.md [--force]   # parse tasks; --force resets all state
    python dev/board.py claim --session S                    # next task (lowest wave, then id, deps done); exit 3 if none
    python dev/board.py release ID [--reason R]              # back to open
    python dev/board.py fail ID --reason R                   # back to open; stuck after 2 fails
    python dev/board.py note FACT|FAIL|NOTE|SPEC-GAP ID TEXT [--basis B] [--session S]   # text <= 400 B, basis <= 200 B
    python dev/board.py tail --session S [--all]             # journal entries since your cursor
    python dev/board.py status                               # table + counts
    python dev/board.py integrate ID --branch B              # merge --no-ff into main, run tests/, push; rolls back on failure
    python dev/board.py ci [--sha SHA]                       # GitHub Actions runs on main; exit 0 ok / 1 failed / 2 pending or none
    python dev/board.py done ID [--sha S]                    # mark done by hand (controller tasks: 16, 25)

Plan format: `### Task N: title` headings (`**RISKY ...**` in the title or bold in the wave table = risky), plus the `| Wn | ... |` wave table. The plan has no dependency lines, so deps are: every task of the previous wave, plus `a -> b` chains inside a wave cell; an optional `**Depends on:** Task 3, 4` line in a task section overrides that. Titles containing "controller task" are never claimed by peers.
`integrate` works in the main worktree (first in `git worktree list`), which must be on `main` and have no tracked changes; untracked files are ignored. Integrations are serialised with their own lock, so `claim`/`note` stay responsive while tests run (timeout 20 min). `ci` reads the token from `~/.config/artel-dev/github-token`; errors exit 4.
Tests: `python -m unittest discover -s dev -q`
