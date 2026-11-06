---
status: active
type: reference
---
# How This Assistant Works

A short map of {{assistant_name}}, for {{name}} and for {{assistant_name}} itself. Written {{date}}.

## The parts
1. **The agent** (Claude Code): reads and edits files, runs commands on this computer, searches the web.
2. **The vault** (this folder, plain markdown, open it in Obsidian): the assistant's memory. It reads `VAULT-INDEX.md` first in every session, and saves what a future session needs.
3. **The boot file** (`CLAUDE.md`, one folder up): who the assistant is, how it talks, what it may do alone, what it must ask about, and the rules that never lapse. Edit it with {{name}}'s say-so only.
4. **The work list** (`Active Priorities.md` for people, `scripts/intent_store.py` for the machine): every item has a tier, a value and a lease; closing one needs evidence. Sensors (`sensor_deadlines.py`, the daily reflection) add items on a timer through the job runner (`scripts/job_runner.py`, registered with your computer's scheduler), and a health check (`scripts/harness_health.py`) alarms when the loop stops.
5. **The safety layer**: anything another person would receive (message, email, post) is blocked by `scripts/send_gate.py` until {{name}} approves that exact text by typing `! python scripts/approve.py "<the text>"`. The assistant cannot approve its own sends. It has no payment access and no secrets in notes.
6. **Per-turn hooks** (small scripts Claude Code runs by itself, no AI involved): on every message `prompt_context_hook.py` puts the real date and time, `logs/focus.md` and the next work item in front of the assistant, and `recall_hook.py` adds the best matching memory lines when the message reads like a question; after a web fetch `injection_prescan_hook.py` warns if the page tries to give orders; when the assistant stops, `keep_going.py` holds the turn open once if it promised a next step, `claim_gate.py` logs whether "done" claims had tool evidence, and `handoff_write.py` leaves `logs/handoff.md` for the next session. Each has an off switch file in `logs/`.
7. **Helpers** (optional, set up by {{name}}): cheap or free models can do routine writing and code jobs. See `Free Helper Models (optional).md`.

## What watches over it (no AI involved, all in `scripts/jobs.json`, run by `job_runner.py` every 5 minutes)
Each watcher adds an ask-first item to the work list and a line to `logs/alerts.txt` when it finds something; none of them deletes or changes your files.
- **Health check** (`harness_health.py`, every 15 min): eight yes/no checks on the loop itself, for example "is the job runner still alive".
- **Integrity check** (`integrity_check.py`, every 15 min): hashes `CLAUDE.md`, the hook settings, the gate scripts and `jobs.json`. If one changed behind the assistant's back it raises an item. After a change you approved, run `python scripts/integrity_check.py --rebaseline`.
- **Secret scan** (`secret_leak_scan.py`, daily): looks through the vault and scripts for passwords, API keys and tokens left in plain files. It reports the place, never the value.
- **Ransomware canary** (`ransomware_canary.py`, every 30 min): plants decoy files in the vault and raises an alarm if one is touched or if more than 20 notes change within five minutes.
- **Startup audit** (`persistence_audit.py`, every 6 hours): lists what starts by itself on this computer (login programs, scheduled tasks) and flags new or changed entries.
- **Program check** (`process_baseline.py`, hourly): learns which programs normally run for three days, then flags a new program, or a known name such as `svchost.exe`, running from a Temp or Downloads folder. Only the name and the kind of folder are written down.
- **Protection audit** (`hardening_audit.py`, weekly): reads whether the computer's own protections are on (Defender, firewall, UAC and drive encryption on Windows; firewall, FileVault and Gatekeeper on macOS; firewall and disk encryption on Linux). A protection that is off becomes one ask-first item per month; you may decide it is not wanted.
- **Vault check** (`vault_check.py`, daily): broken `[[links]]`, notes that no index lists, notes without a `status:` line, daily notes with the wrong name. It never edits a note.
- **Vault snapshots** (`vault_snapshot.py`, every 30 min, needs git): a local history of your notes, so an accidental edit can be undone. It only ever uses a repository of its own inside the vault folder.
- **Security digest** (`security_digest.py`, weekly): one short note, `Security Digest.md`, with the send gate's self-test, the switches that are set, the alerts of the week, the open items from the watchers and how many sends you approved.
- **Autopilot** (`autopilot.py`, every 10 min, **off until you delete `logs/autopilot_off.txt`**): when you are away and a safe item is waiting, runs one capped headless session to work it.
- **Send-gate canary** (every 6 hours): proves the send gate still blocks what it should.
Tools you can run by hand: `script_doctor.py check <file.py>` (syntax errors, wrong-escaped Windows paths, failing sibling tests), `script_index.py` (find an existing script before building a new one), `quarantine.py` (outside text held until you promote it; nothing outside text becomes memory without a look).

## How a normal day goes
{{name}} writes what they need. The assistant answers in {{voice}} style, does the local work it is allowed to do alone, asks about anything on the ask-first list, writes down what a later session will need, and ends with the next step or a question.

## Things you can add later (ask {{assistant_name}} to walk you through each, one at a time)
- **More sensors:** the deadline sensor reads `Deadlines.md`; to watch {{watch}} or anything else, write a small script that adds work items through `intent_store.py` and add a row to `scripts/jobs.json`. Every script gets tests.
- **A private chat channel** so you can talk to it from your phone. A Discord channel (plus read-only mail and calendar tools and game mode) ships as an optional extra, switched off until {{name}} sets it up; see `Optional Extras.md`. Telegram is not included.
- **Free helper models** through a local gateway for routine jobs. Not included; see `Free Helper Models (optional).md`.

## How to look at the loop
`python scripts/harness_health.py --dry-run` (the eight invariants), `python scripts/intent_store.py list` (what is waiting), `python scripts/parity_test.py` (seven end-to-end checks on a copy), `logs/alerts.txt` (what the machinery wanted you to know).

## How to check it is working
Ask {{assistant_name}}: "what did you do since my last message, and show me the evidence". It should name files or commands, not just say it is done.
