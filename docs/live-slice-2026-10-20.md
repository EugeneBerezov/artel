# Live slice check — 2026-10-20 (Task 16)

Controller run, owner consent Q8 (haiku, ≤ 2 tasks). artel at `820091a`+ built as a pyz
(`tools/build_pyz.py`), Claude Code 2.1.305, Linux. Toy repo: `calc.py` (`add`), `test_calc.py`, a stand-in
`artel/` package that writes `TOY_ARTEL_RAN` when imported; `artel.toml` from `config.render_toml()` with
`max_agents = 1`, every `models.*` = `haiku`, `commands.verify = ["python3", "-m", "unittest", "-v"]`,
`commands.project_tools = ["Bash(python3 -m unittest *)"]`, `profiles.common` = the default list plus
`"--setting-sources", "project,local"`.

## Result

`artel add "Add a subtract function with a test"` → `artel run -n 1` → **done** in 31 s
(writing 21:53:15 → reviewing 21:53:35 → done 21:53:46). `artel stop` ended the runner with exit 0; no
`claude -p` process left (`pgrep -af "claude -p"` empty).

| run | outcome | turns | wall | cost | context first/last | usage (in / out, cache read / create) |
|---|---|---|---|---|---|---|
| write r0 | ready | 9 | 15.0 s | $0.0068 | 22 132 / 24 409 | 14 / 1 607, 115 597 / 24 405 |
| review r1 | pass (1 minor) | 4 | 5.2 s | $0.0016 | 22 391 / 23 259 | 4 / 637, 41 520 / 4 126 |
| verify | green | — | < 1 s | — | — | — |

Integration commit `ada07b0` ("Add subtract function with a test", written by the agent); `artel/1` deleted;
the main worktree clean. The writer ran only `cat/ls/git status` (read-only), `python3 -m unittest -v`,
`git add && git commit` and `artel note`; no permission denial. The agent's `artel note FACT …` reached the
runner's board (entry `agent:write`), and the toy `artel/` package did not run through the shim (check d).
`TOY_ARTEL_RAN` appeared in the slot only during the probe run below, from `python3 -m unittest` discovery
importing the slot's own `artel` package — the project's code, as intended; `leave_slot` removed it.

Eight haiku calls in total (the task's two plus six probes below): **$0.026**. The account showed
five-hour 0.36 and seven-day 0.31 utilization.

## Stream fields read by `agent_claude`

All present with the shapes the adapter expects: `system/init.session_id`; one `rate_limit_event` per run
with `rate_limit_info.{status: "allowed_warning", resetsAt, rateLimitType: "seven_day", utilization,
unifiedWindows.{five_hour, seven_day}.{utilization, resetsAt}}`; `assistant.message.usage`;
`result.{structured_output, permission_denials, session_id, api_error_status: null, usage, total_cost_usd,
num_turns, duration_ms}`. **PD-23 confirmed:** several `assistant` lines share one `message.id` (2–3 per id
in every run); the adapter keeps the last usage per id. New record types not read by the adapter:
`system/thinking_tokens`, `system/permission_denied` (`tool_name`, `tool_use_id`, `decision_reason_type`
`asyncAgent` / `subcommandResults`, `decision_reason`). `result.stop_reason` is `tool_use` (the
`StructuredOutput` tool). **PD-40:** no limit hit occurred — the 429 assumption is not observed and stays
unverified.

Every run prints one stderr line from the CLI's schema validator:
`strict mode: use allowUnionTypes to allow union type keyword at "#/properties/closed/items" (strictTypes)`
(`closed.items` and `duplicate_of` use type unions). Harmless; output is still validated and returned.

## Owner decisions (a)–(i)

- **(a)** The writer edited and committed in `.artel/slots/artel-1` under `acceptEdits` with no prompt and no
  denial. Task 30's default holds.
- **(b)** No limit hit: "429 assumption not observed".
- **(c)** With `--setting-sources project,local` + `--strict-mcp-config`: `mcp_servers = []`, `skills = []`,
  `slash_commands = []`, plugins = three built-ins only (`cc-plugin-agents-md`, `cc-plugin-telemetry`,
  `cc-plugin-plugin-authoring`); no hook output. One extra no-schema call **without** `--setting-sources`
  and without `--strict-mcp-config` loaded the claude.ai MCP server, 70 skills and 106 slash commands (auth
  and tools worked in both). No Telegram plugin loaded in any child. **But the tool list is wide in every
  profile:** besides Bash/Edit/Write/Read/Glob/Grep the agent gets `SendMessage`, `ListAgents`,
  `PushNotification`, `RemoteTrigger`, `CronCreate/Delete/List`, `ScheduleWakeup`, `Monitor`, `Task`,
  `Workflow`, `WebFetch`, `WebSearch`, `EnterWorktree`, `DesignSync`, … and `system/init` carries a
  `messaging_socket_path`: an unattended agent could message the owner's interactive Claude Code sessions on
  the same machine or start background work artel does not see. **The PD-55 probe argv lists 16 tools**
  (the `--disallowedTools` list does not cover these) — the pass criterion "no tools" fails.
  Verified fix: `--tools "Bash,Edit,Write,Read,Glob,Grep"` gives exactly those plus `StructuredOutput` and
  still returns `structured_output`; `--tools ""` gives the probe an empty tool list and a normal answer.
  → **Task 44**.
- **(d)** Isolated shim: the agent's `artel note` reached the runner's artel; nothing from the toy package
  ran through the shim. Pass.
- **(e)** Probing reviewer (`profiles.reviewer`, `acceptEdits`): wrote `probe.txt` in the slot and ran
  `python3 -m unittest -v` without a prompt. Pass.
- **(f)** Reviewer `git add -A && git commit …` → denied (`permission_denials`, `asyncAgent`: "no approval
  surface"). **Pass.**
- **(g)** Reviewer `sed -i` on `calc.py`, `mv test_calc.py moved_test.py`, `rm calc.py` and
  `rm node_modules/x.js` (a `slot_keep` path) all ran without a prompt — expected under `acceptEdits`
  with `--permission-prompts none` (read: these Bash commands are auto-accepted edits). `leave_slot` then
  restored the tracked files and removed the untracked ones; `node_modules/x.js` stayed deleted (kept paths
  are not restored). The shared `.git` is protected only by Task 39's repository check (not built yet).
  Recorded for the README.
- **(h)** Reviewer Write to `../outside.txt` → denied ("Path is outside allowed working directories").
  **Pass.**
- **(i)** `git log -1 --output=x.txt` → denied in the reviewer run and in an attacker-profile run
  (`profiles.attacker` built by hand: Task 39 wires it); the attacker also has no Write tool. **Pass.**

(f), (h), (i) pass: Task 39's defaults stand.

## Mismatches → fix tasks before Task 22

1. **Task 44** — tool allowlists: `profiles.writer`/`reviewer` gain `--tools Bash,Edit,Write,Read,Glob,Grep`,
   `profiles.attacker` `--tools Bash,Read,Glob,Grep`, `profiles.probe` `--tools ""` (replacing its
   `--disallowedTools` list). Removes cross-session messaging, cron/remote triggers, sub-agents, web access
   from unattended agents.
2. Not a fix: the schema union-type stderr warning (cosmetic; noted for Task 39 when it next touches
   `agent_claude.py`).
