# -*- coding: utf-8 -*-
"""Correction ledger: every time the user corrects the assistant, record what was wrong, the truth, the real cause and the fix that removes it, and flag a cause
that has come up before as REPEAT, so the same mistake is fixed at its root instead of being apologised for again.

    python ledger.py add <ref> "<what the assistant said or did wrong>" "<the truth>" "<the cause: a file, a line or a habit>" "<the fix made>"
    python ledger.py candidates [--hours 36]   # the user's recent messages that look like corrections and are not in the ledger yet
    python ledger.py stats [--days 30]         # corrections per day, per cause family, and the REPEAT count
    python ledger.py list [-n 10]              # the last rows
<ref> is anything that finds the moment again: a message id, a time, a short phrase.

Where it writes: the ledger itself is logs/corrections.jsonl (one row per correction). A readable copy goes into the vault note "05 - Resources/Correction Ledger.md"
(--note NAME picks another) as one dated bullet, and ONLY through memory_write.py, the kit's one door into the vault: it refuses secrets, holds instruction-like text in
quarantine and keeps a log. The first write creates the note without frontmatter; add `status: active` and `type: log` at the top once if you want the vault check happy.
Rule: every correction gets a row the same day, with the real cause and a fix; a repeated cause (same family) is flagged **REPEAT**. Metric: corrections per day and REPEAT count.

Cause families group wording that differs but means the same habit (unverified-claim, scope-lock, logging-memory, asking-permission, search-before-build). To use your own,
put {"family-name": "regex", ...} in logs/ledger_families.json; it replaces the defaults. A cause that matches none is "other" and is never flagged REPEAT.

`candidates` reads the user's own turns from the Claude Code transcripts of this folder through pain_miner.py (same folder; see its docstring for --source and --all-projects)
and matches them with a correction regex (--pattern REGEX to change it).
Paths: ROOT is the parent of the scripts folder (HARNESS_ROOT overrides), the vault is ROOT/vault (HARNESS_VAULT), logs are ROOT/logs (HARNESS_LOGS).
Pure core (unit tested): family, clean, make_bullet, seen_refs."""
import argparse
import collections
import json
import math
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

ROWS_NAME = "corrections.jsonl"
FAMILIES_NAME = "ledger_families.json"
DEFAULT_NOTE = "05 - Resources/Correction Ledger.md"
MAX_FIELD = 300
MAX_NOTE_READ = 2 * 1024 * 1024

CORRECTION_RX = r"mistake|wrong|forgot|assum|didn'?t implement|dropped|losing? focus|even after|\bagain\b|not helping|lost your focus|that'?s not|no,? i meant|you should have|i told you"

# Cause wording varies, so repeats are found by family. Order matters: the first family that matches wins.
DEFAULT_FAMILIES = [
    ("unverified-claim", r"verif|unchecked|assum|stale|without check|trusted over|before verifying|diffing|did not check|didn'?t check|field name|read .* as"),
    ("scope-lock", r"scope lock|focus|drop|park|new idea|stopped"),
    ("logging-memory", r"not saved|save|\blog\b|logging|record|status table|vault"),
    ("asking-permission", r"\bask|permission|want me to"),
    ("search-before-build", r"search|already existed|exists"),
]


def get_root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def get_logs():
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()


def get_vault():
    return Path(os.environ.get("HARNESS_VAULT") or get_root() / "vault").resolve()


def load_families():
    """-> [(name, compiled regex)]: logs/ledger_families.json when it holds a usable object, else the defaults. A bad regex is skipped, never fatal."""
    pairs = DEFAULT_FAMILIES
    try:
        data = json.loads((get_logs() / FAMILIES_NAME).read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and data:
            pairs = [(str(k), str(v)) for k, v in data.items()]
    except (OSError, ValueError):
        pass
    out = []
    for name, rx in pairs:
        try:
            out.append((name, re.compile(rx, re.I)))
        except re.error:
            continue
    return out


def family(cause):
    for name, rx in load_families():
        if rx.search(cause or ""):
            return name
    return "other"


def clean(text, limit=MAX_FIELD):
    """One line, single spaces, at most `limit` characters."""
    return " ".join(str(text or "").split())[:limit]


def make_bullet(ref, wrong, truth, cause, fam, fix, repeat):
    return "Correction (ref %s): wrong: %s | truth: %s | cause: %s [%s] | fix: %s%s" % (ref, wrong, truth, cause, fam, fix, " **REPEAT**" if repeat else "")


def read_rows():
    try:
        text = (get_logs() / ROWS_NAME).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def append_row(row):
    lg = get_logs()
    lg.mkdir(parents=True, exist_ok=True)
    with (lg / ROWS_NAME).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def note_text(note):
    """The vault note's text for reading only; '' when it is missing, unreadable or outside the vault."""
    try:
        vault = get_vault()
        p = (vault / note).resolve()
        p.relative_to(vault)
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(MAX_NOTE_READ)
    except (OSError, ValueError):
        return ""


def seen_refs(rows, text):
    """Every ref already in the ledger: from the rows and from '(ref X)' / '(msg X)' marks in the vault note."""
    refs = {str(r.get("ref")) for r in rows if r.get("ref") is not None}
    refs.update(re.findall(r"\((?:ref|msg) ([^)\s]+)\)", text or ""))
    return refs


def remember(note, text):
    """Hand one line to memory_write.py, the only way a script here writes into the vault. -> its result dict; never raises."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import memory_write
        return memory_write.write_fact(note, text, source="script")
    except Exception as e:
        return {"status": "error", "reason": "%s: %s" % (type(e).__name__, str(e)[:120])}


def cmd_add(a):
    ref, wrong, truth, cause, fix = (clean(x, 60 if i == 0 else MAX_FIELD) for i, x in enumerate([a.ref, a.wrong, a.truth, a.cause, a.fix]))
    if not all([ref, wrong, truth, cause, fix]):
        print("every field is needed: ref, wrong, truth, cause, fix")
        return 2
    fam = family(cause)
    rows = read_rows()
    repeat = fam != "other" and (any(r.get("family") == fam for r in rows) or ("[%s]" % fam) in note_text(a.note))
    res = remember(a.note, make_bullet(ref, wrong, truth, cause, fam, fix, repeat))
    status = res.get("status")
    if status == "refused":
        print("not saved: %s" % res.get("reason", "refused"))
        return 1
    append_row({"ts": time.time(), "date": time.strftime("%Y-%m-%d %H:%M"), "ref": ref, "wrong": wrong, "truth": truth, "cause": cause, "family": fam, "fix": fix,
                "repeat": repeat, "vault": status})
    msg = "added" + (" (REPEAT cause)" if repeat else "")
    if status == "quarantined":
        msg += "; the vault line was held in quarantine (it read like an instruction), the row is in logs/%s" % ROWS_NAME
    elif status != "wrote":
        msg += "; the vault note was not updated (%s), the row is in logs/%s" % (res.get("reason", status), ROWS_NAME)
    print(msg)
    return 0


def _miner():
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import pain_miner
        return pain_miner
    except Exception:
        return None


def cmd_candidates(a):
    try:
        rx = re.compile(a.pattern or CORRECTION_RX, re.I)
    except re.error as e:
        print("bad --pattern: %s" % e)
        return 2
    miner = _miner()
    if miner is None:
        print("candidates needs pain_miner.py next to this script")
        return 1
    now = time.time()
    msgs, where = miner.load_user_messages(max(1, math.ceil(a.hours / 24.0)), a.source, a.all_projects, now)
    seen = seen_refs(read_rows(), note_text(a.note))
    n = 0
    for m in msgs:
        if m["ts"] >= now - a.hours * 3600 and rx.search(m["text"]) and m["ref"] not in seen:
            n += 1
            print("%s | %s | %s" % (m["ref"], time.strftime("%H:%M", time.localtime(m["ts"])), miner.redact(m["text"][:130].replace("\n", " "))))
    print("%d correction-like messages not in the ledger" % n)
    if not msgs:
        print("(no user messages found at %s; pass --source PATH or --all-projects)" % where)
    return 0


def cmd_stats(a):
    rows = [r for r in read_rows() if float(r.get("ts", 0) or 0) >= time.time() - a.days * 86400]
    perday = collections.Counter(str(r.get("date", ""))[:10] for r in rows)
    fams = collections.Counter(r.get("family", "other") for r in rows)
    print("%d corrections in %d days (%.2f per day), %d flagged REPEAT" % (len(rows), a.days, len(rows) / float(a.days), sum(1 for r in rows if r.get("repeat"))))
    for fam, n in fams.most_common():
        print("  %-22s %d" % (fam, n))
    if perday:
        print("per day: " + ", ".join("%s=%d" % kv for kv in sorted(perday.items())))
    return 0


def cmd_list(a):
    for r in read_rows()[-max(1, a.n):]:
        print("%s | %s | %s | %s%s" % (r.get("date", ""), r.get("ref", ""), r.get("family", ""), clean(r.get("wrong", ""), 70), " | REPEAT" if r.get("repeat") else ""))
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="ledger.py", description="record user corrections and flag repeated causes")
    sub = ap.add_subparsers(dest="cmd")
    add = sub.add_parser("add")
    for f in ("ref", "wrong", "truth", "cause", "fix"):
        add.add_argument(f)
    add.add_argument("--note", default=DEFAULT_NOTE)
    c = sub.add_parser("candidates")
    c.add_argument("--hours", type=float, default=36)
    c.add_argument("--source", default=None)
    c.add_argument("--all-projects", action="store_true")
    c.add_argument("--pattern", default=None)
    c.add_argument("--note", default=DEFAULT_NOTE)
    s = sub.add_parser("stats")
    s.add_argument("--days", type=int, default=30)
    l = sub.add_parser("list")
    l.add_argument("-n", type=int, default=10)
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if a.cmd == "add":
        return cmd_add(a)
    if a.cmd == "candidates":
        return cmd_candidates(a) if a.hours > 0 else 2
    if a.cmd == "stats":
        return cmd_stats(a) if a.days >= 1 else 2
    if a.cmd == "list":
        return cmd_list(a)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
