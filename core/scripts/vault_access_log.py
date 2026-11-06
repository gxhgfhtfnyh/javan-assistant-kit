# -*- coding: utf-8 -*-
"""PostToolUse hook: logs every Read, Write or Edit that touches a file inside the vault, an access trail that git history lacks
(git shows what changed, not what was merely read). Silent and fast: it never prints, never blocks and never breaks the tool call it is attached to.

Reads the hook JSON from stdin (as UTF-8 bytes), picks the file path out of tool_input (file_path, or notebook_path), and when that path is inside
the vault appends one line to logs/vault_access.jsonl:
    {"timestamp": "2026-10-31T12:00:00", "tool": "Read", "path": "/home/me/assistant/vault/notes/a.md"}
Paths outside the vault, other tools, and bad input are ignored.

Wire it as a PostToolUse hook with the matcher "Read|Write|Edit|MultiEdit|NotebookEdit" (the kit's wiring file does this). Limits: reads done through
Bash, Grep or Glob are not covered, only the file tools above. `session_vault_diff.py` summarises this log.
Vault: the folder in HARNESS_VAULT, default ROOT/vault. Kill switch: a file logs/vault_access_log_off.txt.
The log is cut back to its newest lines once it passes 5 MB.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT.
Pure core (unit tested): extract_path, is_vault_path, make_entry, handle."""
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

LOG_NAME = "vault_access.jsonl"
OFF_NAME = "vault_access_log_off.txt"
PATH_KEYS = ("file_path", "notebook_path")
MAX_STDIN = 8 * 1024 * 1024
MAX_PATH = 1000
MAX_LOG_BYTES = 5_000_000
KEEP_LINES = 10000


def extract_path(payload):
    """The file path a tool call works on, or ''. Anything that is not a dict with a string path gives ''."""
    if not isinstance(payload, dict):
        return ""
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return ""
    for k in PATH_KEYS:
        v = ti.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _norm(p):
    return os.path.normcase(os.path.abspath(str(p)))


def is_vault_path(path, vault=None, cwd=None):
    """True when `path` is the vault folder or inside it (compared after making both absolute, case-folded on Windows; symlinks followed). A relative
    path is taken relative to `cwd` when the hook gave one."""
    if not path or "\x00" in str(path):
        return False
    vault = Path(vault) if vault is not None else Path(VAULT)
    p = os.path.expanduser(str(path))
    if cwd and not os.path.isabs(p):
        p = os.path.join(str(cwd), p)
    candidates = {_norm(p)}
    roots = {_norm(vault)}
    try:
        candidates.add(_norm(os.path.realpath(p)))
        roots.add(_norm(os.path.realpath(vault)))
    except (OSError, ValueError):
        pass
    for c in candidates:
        for r in roots:
            if c == r or c.startswith(r.rstrip(os.sep) + os.sep):
                return True
    return False


def make_entry(tool, path, now=None):
    return {
        "timestamp": (now or dt.datetime.now()).isoformat(timespec="seconds"),
        "tool": str(tool or "")[:60],
        "path": str(path)[:MAX_PATH],
    }


def _trim_if_big(path):
    try:
        if path.stat().st_size <= MAX_LOG_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-KEEP_LINES:]
        tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def handle(payload, logs=None, vault=None, now=None):
    """Log the call when it touched the vault. -> the entry written, or None. Never raises."""
    try:
        logs = Path(logs) if logs is not None else Path(LOGS)
        if (logs / OFF_NAME).exists():
            return None
        path = extract_path(payload)
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        if not is_vault_path(path, vault, cwd):
            return None
        entry = make_entry(payload.get("tool_name"), path, now)
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / LOG_NAME
        _trim_if_big(log)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry
    except Exception:
        return None


def read_stdin_payload():
    """The hook JSON from stdin, read as UTF-8 bytes (at most 8 MB). {} on any problem."""
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN + 1)
        if not raw or len(raw) > MAX_STDIN:
            return {}
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def main():
    try:
        handle(read_stdin_payload())
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
