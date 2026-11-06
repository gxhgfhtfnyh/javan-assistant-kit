# -*- coding: utf-8 -*-
"""Vault recent activity: which notes changed in the last N days, by real file modification time, newest first.

Useful at the start of a session to catch up on edits made in earlier sessions (or by the user in Obsidian) without re-reading everything. It is not the
same as vault_snapshot.py (a git history of the vault) or vault_check.py (is the vault consistent): this only answers "what was touched lately".
Read-only. Hidden folders (.obsidian, .trash) and symlinked files are skipped.

    python vault_recent_activity.py              # the last 1 day
    python vault_recent_activity.py --days 7     # the last week (fractions work: --days 0.25 is six hours)
    python vault_recent_activity.py --limit 20   # at most 20 lines (default 100); the count in the header is always the full count
Exit 0; exit 2 if the vault folder does not exist. Paths: the vault is HARNESS_VAULT (default ROOT/vault).
"""
import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DAYS = 1.0
DEFAULT_LIMIT = 100


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _vault():
    return Path(os.environ.get("HARNESS_VAULT") or _root() / "vault").resolve()


def iter_notes(vault):
    """Every .md file under `vault` (sorted), skipping hidden folders and symlinked files. Yields (path, relative path with forward slashes)."""
    for d, dirs, files in os.walk(vault):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(files):
            p = Path(d) / f
            if f.lower().endswith(".md") and not os.path.islink(p):
                yield p, p.relative_to(vault).as_posix()


def recent_notes(days=DEFAULT_DAYS, vault=None, now=None):
    """-> [(relative path, modification datetime)] for notes modified within the last `days` days, newest first (ties by path)."""
    vault = Path(vault) if vault else _vault()
    cutoff = (time.time() if now is None else now) - max(0.0, float(days)) * 86400
    results = []
    for p, rel in iter_notes(vault):
        try:
            mtime = p.stat().st_mtime
        except OSError:                       # deleted or locked between listing and looking
            continue
        if mtime >= cutoff:
            results.append((rel, dt.datetime.fromtimestamp(mtime)))
    return sorted(results, key=lambda r: (-r[1].timestamp(), r[0]))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="vault_recent_activity.py", description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=DEFAULT_DAYS, help="how far back to look (default %g)" % DEFAULT_DAYS)
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="most lines to print (default %d)" % DEFAULT_LIMIT)
    a = ap.parse_args(argv)
    vault = _vault()
    if not vault.is_dir():
        print("vault folder not found: %s" % vault)
        return 2
    notes = recent_notes(a.days, vault)
    print("=== %d note(s) changed in the last %g day(s) ===" % (len(notes), max(0.0, a.days)))
    limit = max(0, a.limit)
    for rel, mtime in notes[:limit]:
        print("%s  %s" % (mtime.strftime("%Y-%m-%d %H:%M"), rel))
    if len(notes) > limit:
        print("... and %d more (raise --limit to see them)" % (len(notes) - limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
