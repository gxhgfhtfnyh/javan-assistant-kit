---
status: active
type: index
---
# Vault Index

The one file {{assistant_name}} reads first. Keep it short and correct: every folder and every important note is listed here, and a note that is not listed here is one a future session will not find.

Last updated: {{date}} (created by the setup interview).

## Folders
- `00 - Inbox/`: anything new that has not been sorted yet. Empty it when you can.
- `01 - Daily Notes/`: one note per day, named `YYYY-MM-DD`, from the template in that folder. Append only: never rewrite old days.
- `02 - Projects/`: one folder per project, each with its own short index note.
- `03 - Personal/`: notes about {{name}} and the people in their life. See [[About Me]].
- `04 - Archive/`: finished or replaced notes. Move here instead of deleting.
- `05 - Resources/`: reference notes and how-tos:
  - [[How This Assistant Works]]: the map of the parts and how to look at the loop.
  - [[Lessons Pack]]: 51 lessons learned the hard way; add your own at the bottom.
  - [[Autonomy Tiers]]: what is done quietly, done then told, asked first, or never.
  - [[Toolbox]]: every tool, hook and watcher this assistant has, what it does, how to run it and its off switch (read it before saying you cannot do something).
  - [[Experience Notes]]: how connections, scripts and protocols work, and the experience behind them (read once, then use as a reference).
  - [[Field Guide]]: the mistakes, fixes, playbooks and numbers learned in real use (read when something goes wrong or before changing a rule).
  - [[Optional Extras]]: tools that ship switched off (Discord chat channel, voice hook, mail and calendar readers, game mode, repair playbook) and how to turn one on.
  - [[Deadlines]]: the list the deadline sensor reads.
  - [[Recall Probes]]: the questions the memory is tested with (`python scripts/recall_eval.py`).
  - [[Free Helper Models (optional)]]: what free helper models are and how to set them up yourself.
  - `Lessons Candidates.md` (created by the learn step), `Experiment Log.md` (created when you first improve the harness) and `Security Digest.md` (written by the weekly security digest).

## Outside the vault (next to it, in the assistant folder)
- `scripts/`: the work loop and its tests. `logs/`: its state, alerts and off switches. `.claude/`: skills, agents and hook wiring. `CLAUDE.md`: the boot file.

## Notes at the top level
- [[Active Priorities]]: what is being worked on now, what is waiting on {{name}}, what is done.
- [[Interview Log]]: what the setup interview recorded, so nothing is asked twice.

## Rules for keeping this index true
- Add a line here in the same pass as creating a note or folder.
- Update before creating: look for an existing note that already covers it.
- Check counts, links and statuses after any change.
