# -*- coding: utf-8 -*-
"""End-of-session check: which vault files were read or written in the last N minutes, from the access log that the vault_access_log.py hook writes
(logs/vault_access.jsonl). Read-only.

Usage:
    python session_vault_diff.py                  last 120 minutes
    python session_vault_diff.py --minutes 30
    python session_vault_diff.py --json           the same as one JSON object

Files are split into "written or edited" and "read only". If the log does not exist yet, the hook is not wired (see the kit's wiring for
vault_access_log.py). Only the last 5 MB of the log is read. Paths are shown relative to the vault when they are inside it.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT.
Pure core (unit tested): parse_time, recent_touches, summarize, short_path."""
import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

LOG_NAME = "vault_access.jsonl"
MAX_READ_BYTES = 5_000_000
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
DEFAULT_MINUTES = 120


def parse_time(value):
    """-> a naive local datetime from '2026-10-31T12:00:00' (extra precision or a UTC suffix is ignored), or None."""
    try:
        return dt.datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return None


def recent_touches(minutes=DEFAULT_MINUTES, now=None, path=None):
    """-> the log entries from the last `minutes` minutes, oldest first. A missing or unreadable log is []; bad lines are skipped."""
    now = now or dt.datetime.now()
    cutoff = now - dt.timedelta(minutes=minutes)
    p = Path(path) if path else Path(LOGS) / LOG_NAME
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > MAX_READ_BYTES:
                f.seek(size - MAX_READ_BYTES)
                f.readline()
            raw = f.read()
    except OSError:
        return []
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or not isinstance(e.get("path"), str):
            continue
        ts = parse_time(e.get("timestamp"))
        if ts is not None and ts >= cutoff:
            out.append(e)
    return out


def short_path(path, vault=None):
    """The path relative to the vault when it is inside it, otherwise unchanged."""
    vault = Path(vault) if vault is not None else Path(VAULT)
    try:
        p, v = os.path.abspath(path), os.path.abspath(vault)
        if os.path.normcase(p).startswith(os.path.normcase(v).rstrip(os.sep) + os.sep):
            return os.path.relpath(p, v).replace(os.sep, "/")
    except (ValueError, TypeError):
        pass
    return path


def summarize(touches, vault=None):
    """-> {"touches", "files", "by_tool", "written": [(path, n)], "read_only": [(path, n)]}; the lists are most touched first."""
    by_tool = Counter(str(t.get("tool") or "?") for t in touches)
    counts = Counter(short_path(t["path"], vault) for t in touches)
    written_paths = {short_path(t["path"], vault) for t in touches if t.get("tool") in WRITE_TOOLS}

    def ordered(items):
        return sorted(((p, counts[p]) for p in items), key=lambda x: (-x[1], x[0]))

    return {
        "touches": len(touches),
        "files": len(counts),
        "by_tool": dict(by_tool),
        "written": ordered(written_paths),
        "read_only": ordered(set(counts) - written_paths),
    }


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="vault files touched recently")
    ap.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.minutes <= 0:
        print("--minutes must be a positive number")
        return 2
    if not (Path(LOGS) / LOG_NAME).exists():
        print("No vault access log yet (logs/%s). Is the vault_access_log.py hook wired?" % LOG_NAME)
        return 0
    s = summarize(recent_touches(a.minutes))
    if a.json:
        print(json.dumps(s, ensure_ascii=False))
        return 0
    print("=== %d vault touches in the last %d minutes, %d distinct file(s) ===\n" % (s["touches"], a.minutes, s["files"]))
    print("By tool:", s["by_tool"])
    for title, rows in (("Written or edited", s["written"]), ("Read only", s["read_only"])):
        print("\n%s:" % title)
        for p, n in rows:
            print("  %s (%dx)" % (p, n))
        if not rows:
            print("  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
