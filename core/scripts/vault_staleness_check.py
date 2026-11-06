# -*- coding: utf-8 -*-
"""Vault staleness check: lists reference, guide and index notes in the vault that have not been touched for a long time, as candidates to re-verify before
anyone trusts them blind (a memory-decay check). Read-only: it never changes a note.

A note counts when its frontmatter says `type: reference`, `guide` or `index` (the kit's note types; --types changes the list) and its last change is older than N days.
The last change is the later of the file's modified time and a `checked: YYYY-MM-DD` date in the frontmatter, so writing `checked:` after you re-verify a note is enough.
Left out: daily notes and the archive (their age says nothing), the `.obsidian` and `.git` folders, notes without frontmatter, and any folder named with --skip.
Note: a sync or git checkout can reset modified times; if everything looks fresh or everything looks old, that is why.

    python vault_staleness_check.py [--days 90] [--types reference,guide,index] [--skip "Folder A,Folder B"]
    python vault_staleness_check.py --job [--raise]     # for the scheduler: writes logs/vault_staleness.txt, logs one alert line, always exits 0;
                                                        # --raise also adds ONE low-priority work item (not again while one is open)
Paths: ROOT is the parent of the scripts folder (HARNESS_ROOT overrides), the vault is ROOT/vault (HARNESS_VAULT), logs are ROOT/logs (HARNESS_LOGS).
Kill switch for the job: a file logs/vault_staleness_off.txt.
Pure core (unit tested): parse_frontmatter, find_stale_notes, format_report."""
import argparse
import datetime as dt
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

DEFAULT_STALE_DAYS = 90
DEFAULT_TYPES = ("reference", "guide", "index")
DEFAULT_SKIP = (".obsidian", ".git", "01 - Daily Notes", "04 - Archive")
HEAD_BYTES = 8192
FRONTMATTER_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
TYPE_RE = re.compile(r"^type:[ \t]*(.*)$", re.M)
CHECKED_RE = re.compile(r"^checked:[ \t]*(\d{4}-\d{2}-\d{2})", re.M)
REPORT_NAME = "vault_staleness.txt"
OFF_NAME = "vault_staleness_off.txt"


def get_root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def get_logs():
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()


def get_vault():
    return Path(os.environ.get("HARNESS_VAULT") or get_root() / "vault").resolve()


def parse_frontmatter(text):
    """-> (note type, checked-date epoch or None) from the head of a note; (None, None) when the note has no frontmatter block."""
    m = FRONTMATTER_RE.match(text or "")
    if not m:
        return None, None
    block = m.group(1)
    t = TYPE_RE.search(block)
    note_type = t.group(1).strip().strip("'\"").lower() if t else ""
    c = CHECKED_RE.search(block)
    checked = None
    if c:
        try:
            checked = time.mktime(dt.datetime.strptime(c.group(1), "%Y-%m-%d").timetuple())
        except ValueError:
            checked = None
    return note_type, checked


def find_stale_notes(vault=None, stale_days=DEFAULT_STALE_DAYS, types=DEFAULT_TYPES, skip=DEFAULT_SKIP, now=None):
    """-> [{"path", "type", "age_days"}] oldest first. The vault being missing gives []."""
    vault = Path(vault) if vault else get_vault()
    now = time.time() if now is None else now
    cutoff = now - stale_days * 86400
    skip_l = {s.lower() for s in skip}
    types_l = {t.lower() for t in types}
    stale = []
    if not vault.is_dir():
        return stale
    for folder, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d.lower() not in skip_l]                   # names are compared inside the vault only, never the vault's own location
        for name in files:
            if not name.lower().endswith(".md"):
                continue
            path = Path(folder) / name
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    head = f.read(HEAD_BYTES)
                note_type, checked = parse_frontmatter(head)
                if note_type is None or note_type not in types_l:
                    continue                                  # no frontmatter, or a type (person, log, tracker...) whose age does not mean it is stale
                last = max(path.stat().st_mtime, checked or 0)
            except OSError:
                continue
            if last < cutoff:
                stale.append({"path": path.relative_to(vault).as_posix(), "type": note_type, "age_days": int((now - last) / 86400)})
    return sorted(stale, key=lambda s: (-s["age_days"], s["path"]))


def format_report(results, days):
    lines = ["=== %d reference/guide/index note(s) untouched for %d+ days ===" % (len(results), days), ""]
    lines += ["[%dd] (%s) %s" % (r["age_days"], r["type"], r["path"]) for r in results]
    return "\n".join(lines)


def write_report(text):
    target = get_logs() / REPORT_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def alert(count, days, raise_item):
    """One log line (and optionally one low-priority work item) for the stale notes. Never raises."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import notify
        text = "%d reference/guide/index note(s) untouched for %d+ days; list in logs/%s. Re-verify the oldest before trusting them." % (count, days, REPORT_NAME)
        notify.alert(text, source="vault_staleness", intent_kind="vault_stale" if raise_item else None, tier=1, value=2, dedupe="vault-stale", logs=get_logs())
    except Exception:
        pass


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="vault_staleness_check.py", description="list vault notes untouched for N days")
    ap.add_argument("--days", type=int, default=DEFAULT_STALE_DAYS)
    ap.add_argument("--types", default=",".join(DEFAULT_TYPES))
    ap.add_argument("--skip", default=",".join(DEFAULT_SKIP), help="comma-separated folder names to leave out")
    ap.add_argument("--job", action="store_true", help="scheduler mode: write the report to logs, log one alert line, always exit 0")
    ap.add_argument("--raise", dest="raise_item", action="store_true", help="with --job: also add one low-priority work item")
    a = ap.parse_args(argv)
    if a.days < 1:
        print("--days must be 1 or more")
        return 0 if a.job else 2
    if a.job and (get_logs() / OFF_NAME).exists():
        return 0
    vault = get_vault()
    if not vault.is_dir():
        print("no vault folder at %s" % vault)
        return 0 if a.job else 2
    types = [t.strip() for t in a.types.split(",") if t.strip()]
    skip = [s.strip() for s in a.skip.split(",") if s.strip()]
    results = find_stale_notes(vault, a.days, types, skip)
    text = format_report(results, a.days)
    print(text)
    if a.job:
        try:
            write_report(text)
        except OSError:
            pass
        if results:
            alert(len(results), a.days, a.raise_item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
