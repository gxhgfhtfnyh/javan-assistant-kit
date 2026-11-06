---
name: harness
description: "Use at the start of a session, when the user asks what is pending, when you see a NEXT or NONE wake line, or when you run, extend or repair the work loop (work list, job runner, health check, deadline sensor, send gate, memory search)."
---

# The work loop (the harness)

Everything runs on plain files in `logs/` and scripts in `scripts/`. Sensors write facts and work items; the job runner calls them every few minutes with no AI; you only act on what is waiting.

## Every wake and every session
- The session-start hook shows a wake line: `NEXT <id> tier<t> value<v> <what>` or `NONE <why>`. If one is named: claim it, do it, close it with evidence from this turn.
- `python scripts/intent_store.py next` shows the top item. `claim <id> --by live-session` takes it for 20 minutes (a lease). `done <id> --result ok|fail|skipped --evidence "what you saw"` closes it; a close without evidence is refused.
- `python scripts/intent_store.py add --kind <kind> --what "<text>" --tier <0-3> --value <1-5> --dedupe <key>` raises a new item. `list` shows the open ones.
- Tiers: 0 do quietly, 1 do then tell, 2 ask first, 3 never. Tier 2 items are questions for the owner, not work.
- The stop hook holds a turn open while a tier 0 or 1 item of value 3 or more waits; finish it or close it honestly (fail or skipped, with the reason).

## The machinery (no AI involved)
- `scripts/job_runner.py` reads `scripts/jobs.json` and runs due jobs (deadline sensor, health check, learn step, daily reflection, and the safety watchers: integrity check, secret scan, ransomware canary, program check, startup audit, protection audit, vault check, vault snapshots, weekly security digest, autopilot). `--once` runs one pass, `--list` shows the table. The setup registers it with the OS scheduler to run every 5 minutes (`scripts/install_scheduler.py --status` shows how). Each watcher's items appear in the work list like any other; the ones that ask first (tier 2) wait for the owner.
- `scripts/harness_health.py` checks eight invariants (work picked up, world watched, nothing falls through, actions stay inside the tiers, loops close, it learns, the machinery is on, it takes initiative). Red ones become value-5 work items. Run it with `--dry-run` to look without alarming.
- The full list of tools (security watchers, vault cleaners, backups, usage reports, desktop helpers and more) with how to run each is in the vault note `Toolbox` (`vault/05 - Resources/Toolbox.md`); `python scripts/script_index.py` searches them too. Look there before saying you cannot do something.
- Per-turn hooks (no AI involved): on every prompt `prompt_context_hook.py` shows the real date and time, the focus file `logs/focus.md` and the next work item, and `recall_hook.py` puts the best matching memory lines in front of you when the message reads like a question; after a web search or fetch `injection_prescan_hook.py` warns when the returned text tries to give you orders; when you stop, `keep_going.py` holds the turn open once if you promised a next step you can do yourself, `claim_gate.py` logs whether a "done/fixed/saved" claim had tool evidence behind it (and turns an "I can't" into a work item), and `handoff_write.py` writes `logs/handoff.md` for the next session.
- Kill switches are files: `logs/<name>_off.txt` turns a job off; `logs/stop_gate_off.txt`, `logs/send_gate_off.txt`, `logs/loop_halt.txt` stop the hooks and the loop; the per-turn hooks have `prompt_context_off.txt`, `recall_off.txt`, `injection_prescan_off.txt`, `keep_going_off.txt`, `claim_gate_off.txt` and `handoff_off.txt`. Never set a switch to get past a block without telling the owner.

## The send gate
Anything that would send content to another person or service is blocked until the owner has approved that exact content. Show the owner the exact text. They approve it by typing `! python scripts/approve.py "<the exact text>"` in this session. You cannot approve your own sends; the gate also blocks any attempt to edit the approvals.

## Memory
- `python scripts/memory_search.py "question words"` finds lines in the vault. Read `VAULT-INDEX.md` first, then search; do not browse folders.
- `python scripts/memory_write.py` is the only way new facts go in: it refuses secrets and flags a likely conflict with an older fact so you can replace that line instead of adding a stale duplicate.
- `python scripts/recall_eval.py` scores recall against `vault/05 - Resources/Recall Probes.md`. Add a probe when you save an important fact.

## When something looks wrong
Run `python scripts/harness_health.py --dry-run`, read `logs/alerts.txt`, and fix the cause; never silence the alarm.
