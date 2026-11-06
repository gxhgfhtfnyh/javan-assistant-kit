---
name: improve-the-harness
description: "Use when starting, running, judging or writing up an experiment or improvement on this assistant setup itself (hooks, scripts, rules, memory, messaging, agents), or when the owner says research, test, solve or experiment."
---

# Improve the harness like a lab

1. **Problem:** one sentence, in the owner's words if they gave them.
2. **Search first.** Look through `scripts/`, the vault and `Lessons Pack.md` before proposing a build; say what already exists.
3. **Research only what is new:** tell any research agent which notes to skip (`lab-researcher` agent).
4. **Hypothesis with a number:** "change X will move metric M from A to B". No number, no experiment.
5. **Baseline before the change:** run the health check (`harness_health.py`), the recall eval (`recall_eval.py`) or the tests, and write the numbers down.
6. **Change small, with a way back:** back up what you edit; edit code with the file-edit tools, never paths or regexes inside shell heredocs (backslashes get mangled silently). The integrity check watches `CLAUDE.md`, `.claude/settings.json`, the hook and gate scripts and `jobs.json`: after you change one of those with the owner's OK, run `python scripts/integrity_check.py --rebaseline`, otherwise the next check raises an alarm about your own edit.
7. **Break it:** tests for code; mutation checks (change the code on purpose, the tests must fail); a real end-to-end run when possible. Say plainly when only a simulation was run.
8. **Result and verdict:** append one entry to `vault/05 - Resources/Experiment Log.md` (create it if missing): hypothesis, change, before, after, verdict (kept, dropped, needs more data), caveats. Dropped ideas are recorded too.
9. **Report:** one plain sentence first, then the numbers.

Honesty rules: unproven stays labelled unproven; quote the source for any number and mark what could not be read; never write "nobody does X", "always", "never" or "proven" without a check; give reviewers files and logs, not your closing summary; any metric that can be gamed needs a counter-metric.
