---
name: vault-hygiene
description: "Use after creating, changing, moving or deleting any note in the vault, when saving what a future session needs, and when the vault index or a count might be stale."
---

# Vault hygiene

The vault is the assistant's memory. Stale information is worse than none, so keep it correct without being asked.

1. **Update before creating.** Search first (`python scripts/memory_search.py "words"`); extend the note that exists. Delete what you replaced; archive instead of deleting anything the owner wrote.
2. **New facts go through the funnel:** `python scripts/memory_write.py` refuses secrets and flags a likely conflict with an older fact, so replace that line in place instead of adding a stale duplicate.
3. **After any change, fix every place it shows up:** the folder's index note, `VAULT-INDEX.md`, counts, statuses, checkboxes, cross-links. A note the index does not list is lost to the next session.
4. **Daily notes are append-only.** One file per day, a `## Session N` section per session; log builds, fixes and decisions as they happen.
5. **Save at the moment of change,** not at the end: the vault note, today's daily note, and the boot file only when there is a new always-on rule. Read it back to verify.
6. **Add a recall probe** to `vault/05 - Resources/Recall Probes.md` for any important fact, then run `python scripts/recall_eval.py` and keep it at 80 percent or better.
7. **Never store secrets** in a note. Name the Credential Manager item (or keychain entry), never the value.
8. **One index first.** Start every session at `VAULT-INDEX.md` and recall by searching, not by browsing folders.
