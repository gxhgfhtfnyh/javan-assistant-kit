# Personal AI assistant starter kit

A plug-and-play start for your own assistant, built from a personal setup that ran for weeks. One command starts a short interview (about 15 questions); at the end you have your own folder with:

- **A boot file** (`CLAUDE.md`): who the assistant is, how it talks, what it may do alone, what it must ask about, and the rules that never lapse.
- **A memory** (`vault/`): an index, active priorities, daily-note template, About Me, an interview log, a lessons pack of 51 hard-won lessons, autonomy tiers, deadlines, recall probes.
- **The work loop** (`scripts/`): a work list with tiers, a job runner, a health check with eight invariants, a deadline sensor, a learn step, a daily reflection, stop and session hooks, per-message hooks (the real time and a focus file in front of the assistant on every message, memory lines recalled when you ask a question), a keep-going stop hook that stops it ending a turn on a promise, a log of whether "done" claims had evidence, a warning when a web page tries to give it orders, a handoff file for the next session, a send gate that holds anything another person would receive until you approve it, a memory search and a memory write funnel, a recall test, a leak scan, and tests for every script.
- **Skills and agents** (`.claude/`): how to run the loop, verify before claiming, handle outside content safely, keep the vault clean, improve the harness like a lab; a claim auditor and a research agent.
- **A parity test** (`python scripts/parity_test.py`): seven checks, each on its own fresh copy of your folder, that prove the loop, the memory and the safety hold actually work.
- **Quiet safety watchers** (no AI involved, run by the job runner): a file integrity check on the boot file, hooks and gate scripts; a secret scan; a ransomware canary; an audit of what starts by itself on the computer; a check of running programs; a vault consistency check; and an autopilot that ships switched off.
- **A toolbox the assistant knows how to use** (`vault/05 - Resources/Toolbox.md`, 156 scripts with tests): more security watchers (new programs, network and listening ports, trusted-binary hashes, Defender exclusions, clipboard secrets, commit-time secret scan), vault cleaners (stale and duplicate notes, link suggestions, daily-note and priorities helpers), backups with a test-restore, usage and transcript reports, goal and outcome tracking, desktop helpers (Windows) and more. Each says what it does, how to run it, and its off switch; the ones that act on the computer are dry-run, opt-in or manual only. Not included: chat bots (Discord, Telegram), mail and calendar readers and free-model gateways, which need your own accounts.
- **A finished setup, not a to-do list** (`finish_setup.py`): Obsidian installed and pointed at your vault (daily notes already configured), the scheduler registered and checked, the folder trusted, the assistant launched.

## Get it with your own Claude Code
Open Claude Code and say:

> Clone `https://github.com/gxhgfhtfnyh/claude-s-weird-thingy` into my home folder as `assistant-kit`, then tell me the one command to run next.

(If you are a Claude Code session and were asked to get this kit: run `git clone https://github.com/gxhgfhtfnyh/claude-s-weird-thingy assistant-kit` in the user's home folder and stop there. Do not run the setup inside your own session. Tell the user to open a **new terminal**, go into that folder and run `.\start.cmd` on Windows or `sh start.sh` on macOS/Linux: that starts the interview in the kit folder, where its permissions and trust are already prepared.)

Without a Claude to ask, the same thing by hand (Windows PowerShell): `git clone https://github.com/gxhgfhtfnyh/claude-s-weird-thingy "$HOME\assistant-kit"; cd "$HOME\assistant-kit"; .\start.cmd`; macOS/Linux: `git clone https://github.com/gxhgfhtfnyh/claude-s-weird-thingy ~/assistant-kit && cd ~/assistant-kit && sh start.sh`. You need Git, Python 3.10 or newer, and Claude Code signed in once (`start.cmd` offers to install Python with winget).

## What you need
- Windows, macOS or Linux.
- [Claude Code](https://claude.com/claude-code), signed in once, and a Claude plan (Claude Code is included in Claude Pro; check the price page) or API credit.
- Python 3.10 or newer. On Windows, `start.cmd` offers to install it for you with winget; pytest and Obsidian are installed by the setup.

## From a bare PC (nothing installed yet)
Windows, in PowerShell (no Administrator needed):
1. Install Claude Code: `irm https://claude.ai/install.ps1 | iex`. It shows no progress; wait until the prompt comes back.
2. Close PowerShell, open a new one and run `claude --version` to check it worked.
3. Install Git: `winget install Git.Git`, then close and reopen PowerShell so it is found.
4. Run `claude` once and sign in in the browser that opens (a paid Claude plan or an API console account; the free plan does not include Claude Code). Type `/exit` when it says hi.
5. Get this kit into your home folder as `assistant-kit`: `git clone <repository address> "$HOME\assistant-kit"` (or unzip the zip you were given there).
6. `cd "$HOME\assistant-kit"` and run `.\start.cmd`. If Python is missing it offers to install it: say yes. Then the questions, one yes, about 10 minutes.

macOS / Linux: install Claude Code with `curl -fsSL https://claude.ai/install.sh | bash`, sign in with `claude` once, then `cd` into this folder and run `sh start.sh`. (Install commands are from Anthropic's setup page: https://code.claude.com/docs/en/setup.)

## Start (one command)
- **Windows:** open this folder and double-click `start.cmd`, or in a terminal opened here run `.\start.cmd` (PowerShell) or `start.cmd` (Command Prompt).
- **macOS / Linux:** run `sh start.sh` in a terminal opened here.

It checks that Python and Claude Code are there, marks this kit folder as trusted so the first start does not ask, then starts Claude with "set me up". Answer the questions one at a time (say `ok` to accept a suggestion, `skip` to skip). At the end you give **one yes**, and then there is nothing else for you to do: it builds your folder (in your home folder), installs pytest and Obsidian if they are missing and puts your notes folder into Obsidian, registers one quiet background task (every 5 minutes) and checks that it really ran, marks the folder as trusted for Claude Code, runs the seven self-checks (the parity test), opens your notes in Obsidian and starts your new assistant in a new window. If something cannot be done automatically on your computer it tells you exactly what, once. A report of the run is kept in `logs/finish_setup.txt` in your new folder, and running `finish_setup.py` again skips steps that already finished.

## Updating an assistant you already built
The kit keeps getting new tools and hooks. To bring your assistant up to date without redoing the setup (your notes, your boot file and your own edits stay as they are):
1. In this kit folder: `git pull`
2. See what would change: `python update_assistant.py "<your assistant folder>"` (nothing is changed yet)
3. Do it: `python update_assistant.py "<your assistant folder>" --apply`. An assistant built before this command existed has no record of which scripts you edited, so add `--force-core` to replace the older scripts (each replaced file is copied to `logs/update/<time>/backup/` first).
Then start a new Claude Code session in the assistant folder so it picks up the new hooks. What it did is in `logs/update_report.txt`.

## What it changes outside the new folder (and how to undo it)
- One scheduled task or timer that runs `scripts/job_runner.py --once` every 5 minutes: `python scripts/install_scheduler.py --remove`.
- Windows only: one small file, `usercustomize.py`, in your Python user folder, so background jobs do not flash console windows: `python scripts/headless_subprocess.py --uninstall` (a file that was there before is restored).
- Claude Code's own settings file (`~/.claude.json`): your new folder and this kit folder are marked trusted; the first untouched copy is kept next to it as `.claude.json.bak_starter_kit`.
- Obsidian's own `obsidian.json`: your vault is added to its list of vaults (a dated backup is kept next to it).
- Programs it may install: pytest (pip) and Obsidian (winget, Homebrew or Flatpak).

## How it works day to day
Sensors and jobs run every 5 minutes with no AI. They write facts and work items into `logs/`. Every Claude session starts with the index and the next work item; the stop hook keeps a turn open while important work waits. Nothing that another person would see or receive is sent until you type the approval yourself. Every automatic behaviour has an off switch (a file in `logs/`), and the health check tells you when something is off.

## Optional extras (shipped, but off)
Things a real setup used that you may or may not want. None is wired in by default; nothing starts, connects or sends until you set it up. The vault note `Optional Extras` (also in the Toolbox) says what each does and the one step to enable it.
- **Discord chat channel:** a daemon, a send script with a send gate and a lint, a typing indicator, a queue watcher, quiet hours for unprompted messages, and a voice hook that keeps messages in your own register. Needs your own Discord bot and token (kept in the OS keyring, never in a file).
- **Waking and quiet:** a watcher and a wake filter, a "still working" ping, a repair playbook for known failures.
- **Mail and calendar, read-only:** Gmail through the real API with the read-only permission, and sensors that turn mail that needs an answer and upcoming events into work items. Needs your own accounts.
- **Safety and quiet time:** an emergency kill switch, a network baseline tripwire, game mode with a hook and an automatic switch.
The scripts have tests that use fakes; the Discord, mail and calendar tools were never run against a real service in this kit, so try each with a harmless first use.

## Not included, on purpose
- **Free helper models (OmniRoute and similar).** You set those up yourself; the vault note `Free Helper Models (optional)` explains what they are, the rules that kept them safe, and what to expect.
- **A chat channel switched on.** The approval gate and the work loop work without one. The Discord channel (daemon, send script with its lint, typing, queue watcher, quiet hours, voice hook), read-only mail and calendar tools, game mode and a few more ship as *optional extras*: the scripts and tests are in `core/scripts`, but no hook is wired and every timed job is disabled until you choose to enable it (see below).
- **Anything personal about the person who made this kit.** A leak scan is part of the kit and part of the parity test.
- **A tuned memory.** The assistant starts with an empty memory plus the Lessons Pack and earns the rest from your corrections. It will not be as good as a setup that has run for weeks until it has learned from you.

## Honest notes
- The kit gives the same rules, the same loop and the same safety checks as the setup it came from. How well it works also depends on the model and plan you use, and on you correcting it.
- The original setup's own numbers are one week of data; do not expect more autonomy than the health check measures.
- Never give it payment access, and never put passwords or keys in its notes.

## For the kit's maintainer
`generate.py` (answers + templates + `core/` to a folder), `questions.json`, `structure.json`, `templates/` (vault notes and the boot file), `core/` (scripts, tests, skills, agents, hook wiring), `personas/` (fake answers for tests), `tests/`. Run `python -m pytest -q tests core/scripts`. Before sharing the kit, run the leak scan with your own private denylist (a file of your names, ids and paths that is NOT in the kit): `python core/scripts/leak_scan.py . --denylist <your file> --ignore core/scripts/test_leak_scan.py`.
