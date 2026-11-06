---
name: setup
description: Use when the user says "set me up", "setup" or "start", or opens this folder for the first time. Runs the short interview and builds their personal assistant folder (boot file and a vault of notes) with generate.py.
---

# Setup interview

Goal: ask the questions in `questions.json`, one at a time, then build the user's own assistant folder from their answers. Keep every message short and friendly.

## Before you start
1. If `answers.json` exists, read it: those questions are already answered, do not ask them again. Say "welcome back" and continue from the first unanswered question. If the assistant folder already exists, say so and ask whether to stop or to rebuild with `--force` (the generator never deletes anything).
2. Read `questions.json` (rounds, questions, defaults, choices). Say the `intro` text once.

## The interview
- Ask **one question at a time**, in the order of the rounds. Say the round title when a new round starts.
- Show the suggested answer and the choices, like: "Which computer do you use? (Windows, macOS or Linux; suggested: Windows)".
- "ok" or "default" means take the suggestion. "skip" means leave it empty. Anything else is the answer.
- After every answer, write it into `answers.json` straight away (a JSON object of question id to answer text), so nothing is lost if the session stops.
- Required questions (the name) cannot be skipped.
- If the user pastes a password, key or token, do not save it. Say you only need the name of the account or tool, never the secret.
- Never make up an answer for them.

## Before building
Show a short summary: the answers (marking the defaults), the folder you will create (the `vault_path` answer), and what goes inside it: the boot file `CLAUDE.md`; the vault (`VAULT-INDEX.md`, Active Priorities, the daily note template, About Me, Interview Log, and in Resources the Lessons Pack, Autonomy Tiers, How This Assistant Works, Deadlines, Recall Probes, the helper-models note) set up for Obsidian; the work loop in `scripts/` with its tests; the quiet safety watchers (a send gate that holds anything another person would receive, a file integrity check, a secret scan, a ransomware canary, a startup-program audit, a vault consistency check; the autopilot ships switched off); skills and agents in `.claude/`; hook wiring in `.claude/settings.json`.
Then say, in plain words, what ONE yes covers so nothing needs doing by hand afterwards: "If you say yes I will build the folder, install the small things it needs (pytest, and Obsidian if it is not installed), put your notes folder into Obsidian, register one quiet background task that runs every 5 minutes and check that it really ran, mark the folder as trusted for Claude Code, run the seven self-checks, open your notes in Obsidian and start your new assistant in a new window. On Windows it also puts one small file (usercustomize.py) in your Python user folder so that background jobs do not flash console windows; it can be removed again with `python scripts/headless_subprocess.py --uninstall` in the new folder." Ask "shall I build it and set everything up?" and **wait for a yes**. After that yes, do not ask for anything else.

## Build
Run every command on its own, never chained with `&&`, `;` or `|` and without a `cd` (you are already in the kit folder): a chained command is not covered by the permission rules and would stop to ask.
1. **Check Python first.** Run `python --version`; if that is missing or older than 3.10, run `python3 --version`, then `py -3 --version`. If none is 3.10 or newer, stop and tell them plainly to install Python 3.10 or newer from python.org (tick "Add Python to PATH" on Windows), then say "ok" to continue. Below, `<python>` means exactly the command that worked (`python`, `python3` or `py -3`, typed as a bare command, never as a full path, so that it needs no permission prompt).
2. **Pick the output folder.** Take the `vault_path` answer. A relative name (like `my-assistant`) goes in the user's **home folder** (for example `~/my-assistant`), not next to the kit and not in Downloads, Documents or Desktop: some systems keep background tasks out of those folders. Expand a leading `~`. If the parent is not writable or the folder already holds other files, say so and ask for another name.
3. Run `<python> generate.py --answers answers.json --out "<that folder>"`. It prints a JSON report of what it created and skipped, or `error: ...` and stops.
4. **Check with real evidence, not memory:** run `<python> generate.py --verify "<that folder>" --answers answers.json`. It prints one `ok:` or `FAILED:` line per fact (the boot file names them and the assistant, no `{{` left anywhere, every script has a test, the hooks start a real program) and ends with VERIFIED or NOT VERIFIED. Do not read the new folder yourself. If it says NOT VERIFIED or the build printed an error, stop: tell them the build failed, show the exact lines, and do not call it done.
5. **Finish the setup in one command:** `<python> finish_setup.py --out "<that folder>" --yes`, run with the Bash tool's timeout set to 600000 milliseconds (it installs software and runs the self-checks, which takes a few minutes). It installs pytest if missing, installs Obsidian if missing (winget, Homebrew or Flatpak) and puts the vault into it, registers the 5-minute job runner and checks that it ran, makes background Python jobs run without console windows (Windows), runs the parity test (seven end-to-end checks on a copy), marks the folder as trusted for Claude Code (a backup of Claude Code's settings file is kept), opens the vault in Obsidian and starts the new assistant in a new terminal window. It prints a line for each step as it finishes and a RESULT line at the end, and keeps them in `logs/finish_setup.txt` in the new folder. Show them those lines exactly. If the command was cut off by the time limit, run it again: steps that already finished are skipped.
6. **Be honest about the RESULT.** If it says everything is set up, tell them in two lines that their assistant is open in the new window and the notes are open in Obsidian, and that this window is finished. If a step says FAILED, show the line, say it is not finished, and try to fix the cause yourself (for example run the failed command again and read its error); never call it done while a parity check fails. If a step says MANUAL, it could not be automated on this computer: say exactly what that line says to do, once, and nothing else.

## Be honest about what this is
The kit builds the memory, the rules, the work loop, the safety gate and the tests. It does not include free helper models (the vault note `Free Helper Models (optional)` explains how to set them up yourself), any tuned memory (a private chat channel such as Discord is there as an optional extra, switched off until set up; see `Optional Extras`): the assistant starts with an empty memory plus the Lessons Pack and earns the rest. Its quality also depends on the model and plan. If they ask for something the kit does not include, say so plainly and do not pretend it exists.
