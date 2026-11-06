---
status: active
type: reference
---
# Toolbox

Every tool this assistant has, from the real files in `scripts/` (written {{date}}; a tool you add later is found with `python scripts/script_index.py`, which reads the scripts' own first lines). How connections, scripts and protocols work (and why scripts beat live tool servers for repeated work) is in [[Experience Notes]]; the mistakes and fixes learned in real use are in [[Field Guide]]. Before saying you cannot do something, look here and in `scripts/script_index.py`: a tool for it may exist. Before building a new one, search this note (`python scripts/memory_search.py "<what you need>"`).

## How to use a tool
- Run it from the assistant folder: `python scripts/<name>.py` (`--help` or `--dry-run` first when it has them; a tool that changes things says so in its first lines).
- The column *Runs* tells whether it runs by itself (a hook Claude Code calls, or a timed job) or only when you run it.
- Every automatic tool has an off switch: a file `logs/<name>_off.txt`. Never switch one off to get past a block without telling {{name}}.
- Tools that act on the computer (stop a program, delete old files, restore a note) are dry-run, opt-in or manual only; read the first lines of the script before enabling one.
- Windows-only tools exit with one short line on other systems.

Count of tools in this note: 156.

## The work loop and the safety layer (always on)

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `approve.py` | Approve one held send, as the owner. Run it yourself in a Claude Code session with the ! prefix (the assistant cannot run it; send_gate.py refuses any command that mentions it). | on request | - | any |
| `autopilot.py` | Autopilot: runs one bounded headless Claude session to work the intent queue when no live session is working. | job every 10 min | autopilot_off.txt | any |
| `hardening_audit.py` | Hardening audit: are the computer's own protections switched on? | job every 10080 min | - | any |
| `harness_health.py` | Harness health: the independent alarm for the harness (2026-10-30). | job every 15 min | - | any |
| `headless_subprocess.py` | Keeps every program a background script starts from opening a visible console window (Windows only; on other systems it does nothing). | on request | - | Windows |
| `heartbeat.py` | Heartbeat module for tracking service heartbeats and restarts. | on request | - | any |
| `install_scheduler.py` | Registers the job runner with the operating system so the work loop runs every 5 minutes without you opening anything, then checks that it really ran. | on request | - | any |
| `integrity_check.py` | Integrity check: has anything that controls the assistant been changed behind its back? | job every 15 min | - | any |
| `intent_store.py` | Intent store: a machine-readable work queue for a harness. Three append-only files plus one small state file, all under the logs folder: events.jsonl facts from sensors, no judgement: {ts, id, source, kind, data} intents.jsonl can | on request | - | any |
| `job_runner.py` | Job runner: one tick, a table, instead of one scheduled task per chore. | on request | - | any |
| `leak_scan.py` | Leak scan: finds personal data and secrets left in a folder before it is shared (your assistant folder, a backup, a zip). | on request | - | any |
| `learn.py` | Learn module: reads outcomes.jsonl and intents.jsonl for the last 24 hours, counts results (ok, fail, skipped), intents closed without evidence, kinds that failed 2+ times, and intents that expired unclaimed; appends a dated secti | job every 180 min | - | any |
| `loop_lock.py` | Loop lock: a file lock with an owner and an expiry. Why: a scheduled job and the live session reach the same files, so two actors can touch the same work item or the same vault note at once. | on request | - | any |
| `notify.py` | One way for the safety and sensor scripts to tell the owner something: a line in logs/alerts.txt, optionally a work item, optionally a desktop toast. | on request | - | any |
| `obsidian_vault.py` | Obsidian Vault Registration Module Finds Obsidian installation, locates its config file, and registers a folder as a vault by editing obsidian.json. | on request | - | any |
| `parity_test.py` | Parity test: seven checks that the assistant folder really works, run on a COPY of the folder so your own logs and notes are never touched. | on request | - | any |
| `persistence_audit.py` | Persistence audit: what starts by itself on this computer, and did that list change? | job every 360 min | - | any |
| `process_baseline.py` | Process baseline sensor: lists running processes with their program paths and maintains a baseline of known process names and the places each has run from. | job every 60 min | - | any |
| `quarantine.py` | Quarantine: keeps untrusted content from becoming memory. Why: content from outside (web pages, other people's messages, email, transcripts, tool output) is data, never instructions, but the memory system would otherwise write dow | on request | - | any |
| `ransomware_canary.py` | Ransomware canary sensor. Plants decoy files in the vault inbox and logs folder, records their hashes in a manifest, and on subsequent runs verifies they are unchanged. | job every 30 min | - | any |
| `run_logged.py` | Runs a Python script with no console window and appends its output to a log file (use it instead of scheduled tasks of the form `cmd /c python.exe x.py >> x.log`, whose cmd window flashes on screen). | on request | - | any |
| `script_doctor.py` | Script Doctor - Static analysis tool for Python files. Usage: python script_doctor.py check <file.py> [more files] Detects: - Syntax errors - pyflakes findings (when pyflakes is installed) - Control characters in source - String c | on request | - | Windows |
| `script_index.py` | Script index for the starter kit. Lists every script in the scripts folder with the first line of its module docstring (--list) and searches the docstrings by keyword, ranking by how many of the words match; this is the 'have I al | on request | - | any |
| `secret_leak_scan.py` | Secret scan: looks through the notes and scripts for text that looks like a password, key or token that ended up in a plain file. | job every 1440 min | - | any |
| `security_digest.py` | Security digest: one short weekly note that shows how the safety layer is doing, so a person can read it in a minute. | job every 10080 min | - | any |
| `send_gate.py` | Send gate: a Claude Code PreToolUse hook that holds anything another person or service would receive until the owner has approved that exact content. | PreToolUse (*), job every 360 min | - | any |
| `sense_initiative.py` | Sense Initiative module: adds ONE tier-1 value-3 intent of kind 'reflection' in each local-time slot 10:00-10:59 and 20:00-20:59 (once per slot, remembered in logs/initiative_state.json) with the text 'Reflect for the owner: read  | job every 30 min | - | any |
| `sensor_deadlines.py` | Deadline sensor: reads vault/Deadlines.md and raises intents for upcoming and overdue items. | job every 15 min | - | any |
| `session_start.py` | SessionStart hook for Claude Code. Prints context at session start: - First 60 lines of vault/VAULT-INDEX.md - First 40 lines of vault/Active Priorities.md - Wake line from intent_store (the 'next' command output) - Count of open  | SessionStart | - | any |
| `stop_gate.py` | Stop hook for Claude Code. Reads hook JSON from stdin (fields: stop_hook_active). | Stop | stop_gate_off.txt | any |
| `vault_check.py` | Vault check: is the memory (the notes folder) still consistent, so a future session can find what it saved? | job every 1440 min | - | any |
| `vault_snapshot.py` | Vault snapshot: keeps a local git history of ROOT/vault as a safety net against accidental edits. | job every 30 min | - | any |

## Memory and recall

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `bug_pattern_scanner.py` | Bug pattern scanner: a retro over the daily notes. It finds the bullets that mention a "real bug" (and, separately, a "near miss"), sorts the real ones into root-cause buckets by keyword, and flags a bucket that repeats 3 or more  | on request | bug_pattern_scanner_off.txt | any |
| `facts_probe.py` | Verified-facts probe: every fact you record about this machine or setup gets a read-only probe, so a fact that has drifted is caught instead of repeated. | on request | - | any |
| `ledger.py` | Correction ledger: every time the user corrects the assistant, record what was wrong, the truth, the real cause and the fix that removes it, and flag a cause that has come up before as REPEAT, so the same mistake is fixed at its r | on request | - | any |
| `memory_search.py` | Memory search: BM25 + rare-word boost over vault .md files. No embeddings, no third-party packages — standard library only. | on request | - | any |
| `memory_write.py` | Memory write funnel: the one door durable facts go through. Appends a dated bullet to a chosen note (frontmatter handling kept), refuses to write outside the vault, refuses text that looks like a secret (api key, password, token,  | on request | - | any |
| `pain_miner.py` | Pain miner: reads the user's own messages to the assistant and finds the moments they had to correct it, step in, repeat themselves or show frustration, then ranks those themes with examples, so you can see where the assistant kee | on request | - | any |
| `prompt_log_hook.py` | UserPromptSubmit hook: keeps a small local log of what the user typed, so the learn step can see how they reacted to finished work. | on request | prompt_log_off.txt | any |
| `recall_eval.py` | Recall evaluation: runs probes against the vault search. Reads vault/05 - Resources/Recall Probes.md whose lines look like 'Q: question text / A: expected substring' For each probe runs search top 5 and checks the expected substri | on request | - | any |
| `session_log_auto.py` | Session log auto: a Stop-hook helper that writes what got done into today's daily note, so the log never depends on remembering. | on request | session_log_auto_off.txt | any |
| `vault_staleness_check.py` | Vault staleness check: lists reference, guide and index notes in the vault that have not been touched for a long time, as candidates to re-verify before anyone trusts them blind (a memory-decay check). | on request | vault_staleness_off.txt | any |

## Keeping the vault clean

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `add_priority.py` | Active Priorities helper: add a task to the right section, close one, and the shared reader of the note's layout. | on request | - | any |
| `archive_completed_priorities.py` | Archive completed priorities: move checked-off items that have sat 30+ days out of Active Priorities into the archive note, so the note read at every start stays short. | on request | archive_completed_priorities_off.txt | any |
| `backlink_suggester.py` | Backlink suggester: which other notes does this note mention in plain text without linking to them? | on request | - | any |
| `cold_start_selftest.py` | Cold-start self-test: are the two files a fresh session boots from still true? | on request | cold_start_selftest_off.txt | any |
| `condense_daily_notes.py` | Condense daily notes: file past months' daily notes into month folders and write one "Highlights" note per month. | on request | condense_daily_notes_off.txt | any |
| `daily_note.py` | Append to today's daily note in one call: an Index line, and/or a new numbered session section, and/or plain text at the end. | on request | - | any |
| `priority_age_tracker.py` | Priority age tracker: how long has each open priority been sitting, so parked work cannot rot unnoticed. | on request | priority_age_tracker_off.txt | any |
| `session_vault_diff.py` | End-of-session check: which vault files were read or written in the last N minutes, from the access log that the vault_access_log.py hook writes (logs/vault_access.jsonl). | on request | - | any |
| `stale_refs.py` | Stale-reference audit: which backticked script, file or folder names in the key notes no longer exist? | on request | stale_refs_off.txt | any |
| `vault_access_log.py` | PostToolUse hook: logs every Read, Write or Edit that touches a file inside the vault, an access trail that git history lacks (git shows what changed, not what was merely read). | on request | vault_access_log_off.txt | any |
| `vault_dedup_check.py` | Vault dedup check: flag content that appears in two different notes, so the no-bloat rule is checked instead of remembered. | on request | vault_dedup_check_off.txt | any |
| `vault_recent_activity.py` | Vault recent activity: which notes changed in the last N days, by real file modification time, newest first. | on request | - | any |
| `vault_size_outliers.py` | Vault size outliers: notes past a line count (default 400) are split candidates, which keeps "no bloat, consolidate" true for note SIZE, not just content. | on request | vault_size_outliers_off.txt | any |
| `vault_todo_scanner.py` | Vault TODO scanner: find unchecked checkboxes (- [ ]) anywhere in the vault OUTSIDE Active Priorities.md. | on request | - | any |

## Checking that the assistant itself is healthy

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `check_backup_locations.py` | Backup locations check: do your backups live on more than one physical disk, and are they recent? | on request | check_backup_locations_off.txt | any |
| `cleanup_scratch.py` | Cleanup of scratch output: plain-text logs and screenshots that pile up for ever. | on request | cleanup_scratch_off.txt | any |
| `disk_space_check.py` | Disk space check: is the disk the assistant lives on running out of room? | on request | disk_space_check_off.txt | any |
| `harness_report.py` | Harness report: one short line a day that makes the work loop visible without anyone asking. | on request | harness_report_off.txt | any |
| `hygiene_audit.py` | Hygiene audit: the bloat checks you would otherwise have to remember to ask for, automated and thresholded, so a problem is flagged unasked. | on request | hygiene_audit_off.txt | any |
| `job_log.py` | Job log: a small JSON-lines run history for scheduled scripts, so "did it work, and how often?" is a query, not a grep. | on request | - | any |
| `lab_report.py` | Lab report: one mechanical summary of the assistant's own logs and checks, so a morning report does not depend on anyone's memory. | on request | lab_report_off.txt | any |
| `process_monitor.py` | Process monitor: CPU and memory of the long-running programs the assistant depends on, with a flag for one that has grown too big (a leak). | on request | process_monitor_off.txt | any |
| `sense_capabilities.py` | Capability sensor: finds out that the assistant can no longer do something BEFORE the user asks for it. | on request | sense_capabilities_off.txt | any |
| `sense_health.py` | Health sensor: notices when a piece of the setup has quietly stopped working and turns it into a work item. | on request | sense_health_off.txt | any |
| `sense_self.py` | Self sensor: reads the assistant's own quality logs back and raises a work item when one moves the wrong way. | on request | sense_self_off.txt | any |
| `sensor_runner.py` | Sensor runner: one job that runs every sensor script and raises an alarm when one keeps failing. | on request | sensor_runner_off.txt | any |

## Security watchers and tools

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `access_review.py` | Access review: did someone get added to a powerful local group? | on request | access_review_off.txt | Windows |
| `afterhours_check.py` | After-hours check: an overnight tamper tripwire. Only inside a quiet window (default 02:00-06:00 local time) it looks for scripts or vault notes that were changed in the last hour while NO assistant session (the `claude` program)  | on request | afterhours_check_off.txt | any |
| `audited_keyring.py` | A thin wrapper around the `keyring` package that writes an access trail: every credential lookup is logged (when, service, username, found yes/no), never the secret itself. | on request | keyring_access_off.txt | any |
| `break_glass.py` | Emergency credential response: for when a compromise is actually suspected, not for routine cleanup. | on request | - | any |
| `canary_check.py` | Canary check: a tripwire for prompt injection and for anything that dumps notes where it should not. | on request | canary_check_off.txt | any |
| `check_permissions.py` | Permission drift check: did the access rules on the assistant's own folders change? | on request | check_permissions_off.txt | Windows |
| `clipboard_scrubber.py` | Clipboard scrubber (Windows only): clears the clipboard a short while after something that looks like a secret was copied. | on request | clipboard_scrubber_off.txt | Windows |
| `credential_age_check.py` | Tracks how long it has been since each credential was last confirmed present, and flags old ones for a rotation look. | on request | credential_age_check_off.txt | any |
| `defender_exclusion_check.py` | Defender exclusion check: did something tell Windows Defender to look away? | on request | defender_exclusion_check_off.txt | Windows |
| `dependency_cve_scan.py` | Dependency CVE scan: runs pip-audit (the Python Packaging Authority's vulnerability scanner) over every Python environment the assistant uses and alerts on known CVEs. | on request | dependency_cve_scan_off.txt | any |
| `injection_known.py` | Known-findings filter for the vault injection scan: tells you only what is NEW since you last reviewed the findings. | on request | - | any |
| `injection_scan.py` | Injection scan: looks through every note in the vault for text that tries to give orders to an AI. | on request | injection_scan_off.txt | any |
| `keepsake_guard.py` | keeps the notes you choose from ever being lost or quietly rewritten. | on request | keepsake_guard_off.txt | any |
| `keyring_audit.py` | Lists the assistant's credentials in the Windows Credential Manager and how long ago each was last looked up through `audited_keyring`, and flags the ones unused for more than 90 days as candidates for rotation or removal. | on request | keyring_audit_off.txt | Windows |
| `known_binaries_check.py` | Known binaries check: a checksum registry for the programs the assistant depends on. | on request | known_binaries_check_off.txt | any |
| `listening_ports_check.py` | Listening ports check: are the assistant's own local servers reachable from the network? | on request | listening_ports_check_off.txt | any |
| `local_binding_check.py` | Local binding check: warns when a server on this computer listens on every network interface (0.0.0.0, ::) or on the LAN address instead of on localhost only. | on request | local_binding_check_off.txt | any |
| `pre_commit_secret_scan.py` | Git pre-commit hook: scans the lines a commit ADDS for secrets and blocks the commit when it finds one. | on request | pre_commit_secret_scan_off.txt | any |
| `privacy_mode.py` | Privacy mode: a switch that makes screen capture refuse. While it is on, desktop_screenshot.py (and everything built on it, like desktop_control.see()) raises an error instead of taking a picture, and the optional privacy_mode_hoo | on request | - | any |
| `privacy_mode_hook.py` | PreToolUse hook: while privacy mode is on, refuse tools that take a picture of the screen. | on request | privacy_mode_hook_off.txt | any |
| `scan_worker_tests.py` | Side-effect scan for test files written by someone else (an AI helper, a contributor, a download): read it before you run it. | on request | - | any |
| `scheduled_tasks_audit.py` | Scheduled tasks audit: do the assistant's own Windows tasks show a console window or run with more rights than they need? | on request | - | Windows |
| `startup_programs_audit.py` | Startup programs audit: what is set to start by itself when this Windows account logs in? | on request | - | Windows |
| `threat_response.py` | Threat response (Windows only): spots suspicious new programs, asks Windows Defender whether they are really malware, and only acts when you switched acting on. | on request | threat_response_off.txt | Windows |
| `typosquat_check.py` | Typosquat check: a quick look at a Python package name before you install it. | on request | - | any |
| `windows_hardening_audit.py` | Windows folder-permission audit: can any local user change the assistant's own files? | on request | windows_hardening_audit_off.txt | Windows |

## Learning from outcomes and goals

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `boot_eval.py` | Boot file eval: does a fresh session that has only CLAUDE.md in front of it still know the rules that can never lapse? | on request | boot_eval_off.txt | any |
| `goals_check.py` | Machine-checked goals: named numbers compared to targets, so "am I done?" is a measurement and not a feeling. | on request | goals_check_off.txt | any |
| `learn_outcomes.py` | Learn step: turns what the user said after each finished work item into a rate per kind of work item. | on request | learn_outcomes_off.txt | any |
| `metric_loop.py` | Metric loop: try a change, measure before and after, keep it or revert it by the number. | on request | - | any |
| `scoreboard.py` | Daily scoreboard: a few self-measured numbers appended as one row a day to logs/scoreboard.csv, to show whether the assistant is getting better or worse. | on request | scoreboard_off.txt | any |

## Cost, usage and transcripts

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `automation_miner.py` | Automation miner: finds work you keep paying tokens for that a small script could do. | on request | - | any |
| `peek.py` | peek: read part of a file cheaply. Reading a whole file, or a big range with sed or cat, puts every line into the conversation; asking for the outline or one named section costs a fraction of the tokens. | on request | - | any |
| `token_audit.py` | where did the tokens of one Claude Code session go? Reads one session transcript (a .jsonl file under <claude config folder>/projects/<project>/) and attributes every API call's usage (fresh input / cache read / cache write / outp | on request | - | any |
| `token_usage_report.py` | Token usage report from Claude Code's own local session logs. | on request | - | any |

## Scripts and development tools

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `dead_script_check.py` | Dead script check: which scripts does nothing mention, and which have not been touched for a long time? | on request | - | any |
| `encoding_guard_lint.py` | Encoding guard lint: finds scripts that print to the console but could crash on a non-ASCII character. | on request | - | any |
| `git_bloat_check.py` | Git bloat check: finds large files sitting in the git history of your repositories. | on request | git_bloat_check_off.txt | any |
| `harness_manifest.py` | Harness manifest: what the assistant folder actually IS, traced from what runs. | on request | - | any |
| `skill_use.py` | How often the installed skills actually get loaded, and whether a nudge made a difference. | on request | - | any |
| `toolbox.py` | Toolbox generator: one short note that tells the assistant which tools exist, built from the real files so it cannot go stale. | on request | toolbox_off.txt | any |
| `worker_review.py` | Mutation review of a test file: do these tests notice when the code is broken? | on request | - | any |

## Backups and scheduled runs

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `backup_vault.py` | Backup: a dated zip of the vault (plus the scripts folder and the boot file), written OUTSIDE the vault, with the newest N kept. | on request | backup_vault_off.txt | any |
| `run_scheduled_prompt.py` | Run one prompt through headless Claude Code (`claude -p`) unattended, for a scheduler to call: it logs the run and raises an alert when the run fails. | on request | run_scheduled_prompt_off.txt | any |
| `scripts_snapshot.py` | Scripts snapshot: a local git history of the scripts folder, plus a tracked copy of the boot file, so a bad edit to any tool (or to the boot file) can be rolled back. | on request | scripts_snapshot_off.txt | any |
| `verify_backup.py` | Verify the newest backup by really restoring it: "the backup ran" and "the backup can be restored" are different claims. | on request | verify_backup_off.txt | any |

## Desktop control (Windows)

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `desktop_control.py` | See -> find -> click or type, through Windows UI Automation (pywinauto) instead of pixel guesses. | on request | - | any |
| `desktop_screenshot.py` | Capture the desktop (all monitors) or one named window to a PNG so the assistant can see what is on screen instead of asking you for a screenshot. | on request | - | any |
| `foreground_window.py` | Which window has focus right now: the title of the foreground window, read from the real Win32 API (GetForegroundWindow), not guessed. | on request | - | any |

## Everyday helpers

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `doc_to_text.py` | Plain-text copies of PDF, PowerPoint and Word files, so a document can be searched or read as text instead of opening it. | on request | doc_to_text_off.txt | any |
| `downloads_sorter.py` | Sort the loose files in one folder (by default your Downloads) into category subfolders. | on request | downloads_sorter_off.txt | any |
| `morning_briefing.py` | Morning briefing: a short plain-text note built from the kit's own data, written to logs/morning_note.txt. | on request | morning_briefing_off.txt | any |
| `set_presence_mode.py` | Presence mode: is the user at home (at the keyboard), away, or asleep? | on request | - | any |
| `transcribe_audio.py` | Offline transcription of a recording (audio or video file) into a markdown note in the vault. | on request | - | any |
| `watch_video.py` | Prepare a video so the assistant can "watch" it: sampled frames as images plus a time-stamped transcript. | on request | - | any |
| `weather_check.py` | One-line weather from Open-Meteo (free, no account, no key). | on request | - | any |

## Hooks (small scripts Claude Code runs by itself)

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `activity_log_hook.py` | PreToolUse + PostToolUse hook: one JSON line per tool call in logs/activity_log.jsonl, so there is a record of what the assistant actually did. | PreToolUse (*), PostToolUse (*) | activity_log_off.txt | any |
| `claim_gate.py` | Claim gate for Claude Code, LOG-ONLY. It never blocks and never prints. | Stop | claim_gate_off.txt, refusal_watch_off.txt | any |
| `doctor_edit_hook.py` | PostToolUse hook: checks a script the moment the assistant writes it, so a bug is fixed in the same turn. | PostToolUse (Write|Edit|MultiEdit) | doctor_edit_off.txt | any |
| `handoff_write.py` | Stop hook for Claude Code: writes logs/handoff.md. One short file (hard cap 2500 characters) holding what a fresh session needs to resume after a crash or a /compact: - the time it was written - the focus file logs/focus.md (cappe | Stop | handoff_off.txt | any |
| `injection_prescan_hook.py` | PostToolUse hook for WebFetch and WebSearch: warns when the text that came back tries to give orders to an AI. | PostToolUse (WebFetch|WebSearch) | injection_prescan_off.txt | any |
| `keep_going.py` | Keep-going Stop hook for Claude Code. Problem it solves: the assistant closes a turn with "next I'm doing X" and the turn simply ends, so X never starts until the user pings again. | Stop | keep_going_off.txt | any |
| `prompt_context_hook.py` | UserPromptSubmit hook: puts the facts an assistant keeps getting wrong in front of it on every prompt. | UserPromptSubmit | intents_auto_off.txt, prompt_context_off.txt | any |
| `recall_hook.py` | UserPromptSubmit hook: recall without being asked. Why: an assistant often answers from the conversation instead of checking its memory. | UserPromptSubmit | recall_off.txt | any |
| `similar_check.py` | PostToolUse hook on Write: when the assistant CREATES a new script or a new vault note, prints ONE line naming the closest existing ones, so it reuses or merges instead of duplicating. | PostToolUse (Write) | similar_check_off.txt | any |
| `skill_nudge.py` | UserPromptSubmit hook: one short line naming the skill or tool that matches what the prompt is about, at most once per skill per 3 hours. | on request | skill_nudge_off.txt | any |
| `tool_heartbeat_hook.py` | PostToolUse hook: every tool call is proof that the live session is working, so it touches the 'claude_session' heartbeat. | PostToolUse | tool_heartbeat_off.txt | any |
| `vault_checkpoint_hook.py` | PostToolUse hook: a short reminder to finish a notes change properly, after the assistant edits the vault or the boot file. | PostToolUse (Write|Edit|MultiEdit) | vault_checkpoint_off.txt | any |

## Optional extras (shipped switched off; see Optional Extras for how to turn one on)

| Tool | What it does | Runs | Switch | System |
|---|---|---|---|---|
| `discord_daemon.py` | Discord bridge daemon: an OPTIONAL chat channel between you and your assistant. | job every 5 min (off) | discord_daemon_off.txt | any |
| `discord_queue_send.py` | Queue a Discord message for discord_daemon.py to send. OPTIONAL chat channel: nothing here runs until you create your own Discord bot and a config file. | on request | discord_queue_send_off.txt, discord_send_lint_off.txt | any |
| `discord_typing.py` | Discord typing indicator keeper (part of the OPTIONAL Discord chat channel). | on request | discord_typing_off.txt | any |
| `game_auto.py` | Automatic game mode (OPT-IN): flips the game mode switch for you so you do not have to say "I am gaming now" and "I am done". | job every 5 min (off) | game_auto_off.txt | any |
| `game_mode.py` | Game mode: one reversible do-not-disturb switch for when the user is in a full-screen game. | on request | - | any |
| `game_mode_guard_hook.py` | PreToolUse hook: while GAME MODE is on, refuse tool calls that would open or focus a window. | on request | game_mode_guard_hook_off.txt | any |
| `gmail_api.py` | Read-only Gmail access through the real Gmail API (no browser scraping). | on request | - | any |
| `kill_switch.py` | Emergency stop: a MANUAL tool for "something looks wrong right now, stop everything while I find out what happened". | on request | autopilot_off.txt, job_runner_off.txt | any |
| `network_baseline_check.py` | Network baseline check: a cheap tripwire for beaconing and data exfiltration. | job every 30 min (off) | network_baseline_check_off.txt | any |
| `queue_check.py` | Queue check: print the new lines of an incoming-message log (default logs/discord_incoming.jsonl, written by discord_daemon.py), once each, by byte cursor. | on request | - | any |
| `quiet_dm.py` | Quiet hours for unprompted, non-urgent messages: held while the owner is asleep, delivered as ONE message afterwards, never lost. | job every 5 min (off) | quiet_dm_off.txt | any |
| `repair_playbook.py` | Repair playbook: known failures get fixed by a tested, safe action instead of waiting for a person to notice. | job every 15 min (off) | repair_playbook_off.txt | any |
| `sense_asks.py` | Ask sensor (OPTIONAL, off until you give it your message log): nothing the user asks falls through. | on request | sense_asks_off.txt | any |
| `sense_life.py` | Life sensor (OPTIONAL, off until you point it at a calendar): turns what is coming up in the user's calendar into work items with a sensible lead time, so the assistant acts without being told. | on request | sense_life_off.txt | any |
| `sense_mail.py` | Mail sensor (OPTIONAL, off until you connect Gmail): raises one work item per unread message that LOOKS like it needs an answer, never a bare count. | on request | sense_mail_off.txt | any |
| `status_ping.py` | Status ping: after a long silence, one short "still working" line, so the user is never left wondering. | job every 5 min (off) | status_ping_off.txt | any |
| `voice_hook.py` | Voice card hook (UserPromptSubmit): keeps the assistant's chat messages sounding like a person texting a friend, in the owner's own register. | on request | voice_hook_off.txt | any |
| `voice_stats.py` | Voice drift meter: measures how your assistant's chat messages sound next to your own, so "it keeps changing how it talks" becomes a number instead of a feeling. | on request | - | any |
| `wake_filter.py` | Wake filter: keeps repeated and noisy lines from waking the session, and never hides anything. | on request | wake_filter_off.txt | any |
| `watcher.py` | Watcher: prints one line per NEW event from a configurable list of sources, so a Claude Code Monitor can wake the session. | on request | watcher_off.txt | any |

## Not included on purpose
- Free helper models / gateways (see `Free Helper Models (optional).md`), a Telegram bot, and anything that needs an account of {{name}}'s: they are set up by {{name}} later, one at a time. The Discord chat channel, the mail and calendar readers, game mode and a few more are in the *Optional extras* section above, shipped switched off (see `Optional Extras.md`).
