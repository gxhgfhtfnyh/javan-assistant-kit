# -*- coding: utf-8 -*-
"""Vault TODO scanner: find unchecked checkboxes (- [ ]) anywhere in the vault OUTSIDE Active Priorities.md.

Active Priorities.md is the system of record for open work, but a stray "- [ ]" can end up in a meeting note, a daily note or a project note and quietly never
be looked at again. This lists every one, with its note and line number, so each can be promoted into the real list (python add_priority.py "...") or ticked off.
Read-only. Boxes inside fenced code blocks (``` or ~~~) are examples, not tasks, and are skipped; so are hidden folders (.obsidian, .trash), symlinked
files and notes over 2 MB.

    python vault_todo_scanner.py             # list them (exit 0; exit 2 if the vault folder does not exist)
    python vault_todo_scanner.py --limit 50  # at most 50 lines (default 200); the count in the header is always the full count
Paths: the vault is HARNESS_VAULT (default ROOT/vault).
"""
import argparse
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRIORITIES_NAME = "Active Priorities.md"
MAX_NOTE_BYTES = 2_000_000
DEFAULT_LIMIT = 200
CHECKBOX = re.compile(r"^\s*[-*+] \[ \]")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


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


def strays_in(text):
    """[(line number, stripped line)] of unchecked boxes in `text`, outside fenced code blocks."""
    out, marker = [], None
    for n, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line)
        if marker is None:
            if m:
                marker = (m.group(1)[0], len(m.group(1)))
            elif CHECKBOX.match(line):
                out.append((n, line.strip()))
        elif m and m.group(1)[0] == marker[0] and len(m.group(1)) >= marker[1] and not line.strip().strip("`~"):
            marker = None
    return out


def find_stray_checkboxes(vault=None):
    """-> [(relative path, line number, text)] for every unchecked box outside the vault's top-level Active Priorities.md, sorted by note then line."""
    vault = Path(vault) if vault else _vault()
    results = []
    for p, rel in iter_notes(vault):
        if rel == PRIORITIES_NAME:
            continue
        try:
            if p.stat().st_size > MAX_NOTE_BYTES:
                continue
            text = p.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        for n, line in strays_in(text):
            results.append((rel, n, line))
    return results


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="vault_todo_scanner.py", description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="most lines to print (default %d)" % DEFAULT_LIMIT)
    a = ap.parse_args(argv)
    vault = _vault()
    if not vault.is_dir():
        print("vault folder not found: %s" % vault)
        return 2
    stray = find_stray_checkboxes(vault)
    print("=== %d stray unchecked checkbox(es) outside %s ===" % (len(stray), PRIORITIES_NAME))
    for rel, n, text in stray[:max(0, a.limit)]:
        print("%s:%d  %s" % (rel, n, text))
    if len(stray) > max(0, a.limit):
        print("... and %d more (raise --limit to see them)" % (len(stray) - max(0, a.limit)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
