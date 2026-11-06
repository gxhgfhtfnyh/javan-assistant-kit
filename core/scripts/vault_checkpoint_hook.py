# -*- coding: utf-8 -*-
"""PostToolUse hook: a short reminder to finish a notes change properly, after the assistant edits the vault or the boot file.

A rule written in a note ("check the index after every change") does not fire by itself; under momentum the steps most likely to be skipped are the
folder-index check and the cross-reference check. So the first time a file inside the vault (or the boot file CLAUDE.md at the assistant's root) is
written or edited, this hook gives the assistant one reminder as additional context: did you check the touched folder's own index and the vault
index for anything now stale, and did you read the change back to confirm it landed? (The vault-hygiene skill holds the full sequence.)

Debounced: after a reminder, no further one for 10 minutes, so a multi-file update is reminded once, not after every edit. The time of the last
reminder is kept in logs/vault_checkpoint_last.txt. Edits to Obsidian's own settings folder (.obsidian) do not count.
Never blocks a tool, never raises, always exits 0. Kill switch: logs/vault_checkpoint_off.txt (the hook does nothing).

Hook config: PostToolUse, matcher "Write|Edit|MultiEdit". Reads the hook JSON from stdin as UTF-8 bytes and prints
{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "..."}} when it fires.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Pure core (unit tested): is_checkpoint_path(path, vault, boot_file), cooldown_active(state_file, now), handle(payload, ...)."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OFF_NAME = "vault_checkpoint_off.txt"
STATE_NAME = "vault_checkpoint_last.txt"
BOOT_FILE_NAME = "CLAUDE.md"
COOLDOWN_MINUTES = 10
WATCHED_TOOLS = ("Write", "Edit", "MultiEdit")
IGNORED_PARTS = (".obsidian", ".trash")

MESSAGE = ("[vault-checkpoint reminder: this looks like a checkpoint-worthy edit of the notes. Before moving on: did you check the touched folder's own "
           "index and the vault index for anything now stale because of this change (counts, statuses, links)? And did you read the change back to "
           "confirm it actually landed? The full sequence is in the vault-hygiene skill.]")


def root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent)


def logs_dir():
    return Path(os.environ.get("HARNESS_LOGS") or root() / "logs")


def vault_dir():
    return Path(os.environ.get("HARNESS_VAULT") or root() / "vault")


def _real(path):
    return os.path.normcase(os.path.realpath(str(path)))


def is_checkpoint_path(path, vault=None, boot_file=None):
    """True for a file inside the vault folder (not in .obsidian / .trash) or for the boot file itself."""
    try:
        if not path or not isinstance(path, str):
            return False
        p = _real(path)
        boot = _real(boot_file if boot_file is not None else root() / BOOT_FILE_NAME)
        if p == boot:
            return True
        v = _real(vault if vault is not None else vault_dir())
        if p == v or not p.startswith(v + os.sep):
            return False
        parts = [x.lower() for x in p[len(v) + 1:].split(os.sep)]
        return not any(x in IGNORED_PARTS for x in parts)
    except Exception:
        return False


def cooldown_active(state_file, now=None, minutes=COOLDOWN_MINUTES):
    """True when a reminder was given less than `minutes` ago. A missing, unreadable or future-dated state file means no cooldown."""
    try:
        last = float(Path(state_file).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    age = (time.time() if now is None else now) - last
    return 0 <= age < minutes * 60


def record_reminder(state_file, now=None):
    """Write the time of this reminder atomically (a temp file in the same folder, then a rename). False when it could not be written."""
    try:
        state_file = Path(state_file)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(state_file.parent), prefix=".vcp-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(time.time() if now is None else now))
        os.replace(tmp, str(state_file))
        return True
    except Exception:
        return False


def add_context_json(text):
    """The JSON a PostToolUse hook prints so that its text reaches the assistant as additional context."""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}})


def read_payload():
    """The hook JSON from stdin read as UTF-8 bytes. {} on any problem."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}                                  # run by hand in a terminal: nothing to read, do not wait
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def handle(payload, logs=None, vault=None, boot_file=None, now=None):
    """-> the JSON text to print, or "" for nothing. Records the reminder time when it fires. Never raises."""
    try:
        logs = Path(logs) if logs else logs_dir()
        if (logs / OFF_NAME).exists() or not isinstance(payload, dict):
            return ""
        tool = payload.get("tool_name")
        if tool and tool not in WATCHED_TOOLS:
            return ""
        tin = payload.get("tool_input") or {}
        path = tin.get("file_path", "") if isinstance(tin, dict) else ""
        if not is_checkpoint_path(path, vault, boot_file):
            return ""
        state = logs / STATE_NAME
        if cooldown_active(state, now):
            return ""
        record_reminder(state, now)
        return add_context_json(MESSAGE)
    except Exception:
        return ""


def main():
    try:
        out = handle(read_payload())
        if out:
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
            sys.stdout.write(out + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
