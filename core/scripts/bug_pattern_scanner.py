# -*- coding: utf-8 -*-
"""Bug pattern scanner: a retro over the daily notes. It finds the bullets that mention a "real bug" (and, separately, a "near miss"), sorts the real ones into root-cause
buckets by keyword, and flags a bucket that repeats 3 or more times as a candidate for a standing rule or a pre-flight check. Whether a bug taught a general lesson stays
a human (or assistant) judgement; this only makes sure real bugs do not stay buried in old notes, never looked at again. Read-only on the vault.

The convention it relies on (write it in your daily notes): one bullet per bug, containing the phrase `real bug` when something actually broke, or `near miss` when it almost did.
    - real bug: the export ran before the file finished writing (race), fixed by waiting for the lock
    - near miss: nearly overwrote the backup; caught because I re-read the path first
Buckets are keyword lists tried in order (a keyword matches from the start of a word: "race" matches "race condition" but not "trace"); the first bucket that matches wins and
anything else is "uncategorized". To use your own buckets put {"bucket name": ["keyword", ...], ...} in logs/bug_categories.json; it replaces the defaults.

    python bug_pattern_scanner.py [--since YYYY-MM-DD]      # every real-bug bullet with its date and bucket
    python bug_pattern_scanner.py --retro                    # counts per bucket, with 3+ flagged
    python bug_pattern_scanner.py --job [--threshold 3]      # for the scheduler: --retro, report saved to logs/bug_retro.txt, one alert + work item per bucket at the threshold, always exit 0
    python bug_pattern_scanner.py --notes DIR                # scan another folder (default: the vault's "01 - Daily Notes", including month sub-folders)
Daily notes are files named YYYY-MM-DD.md. Paths: ROOT is the parent of the scripts folder (HARNESS_ROOT overrides), the vault is ROOT/vault (HARNESS_VAULT), logs are ROOT/logs
(HARNESS_LOGS). Kill switch for the job: a file logs/bug_pattern_scanner_off.txt.
Pure core (unit tested): categorize, find_bug_mentions, retro_counts."""
import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

DAILY_FOLDER = "01 - Daily Notes"
MARKER_RE = re.compile(r"real bug", re.IGNORECASE)
NEAR_MISS_RE = re.compile(r"near.?miss", re.IGNORECASE)
BULLET_RE = re.compile(r"^[ \t]*[-*][ \t].*$", re.MULTILINE)
DAILY_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$", re.IGNORECASE)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CATEGORIES_NAME = "bug_categories.json"
REPORT_NAME = "bug_retro.txt"
OFF_NAME = "bug_pattern_scanner_off.txt"
MAX_NOTE_BYTES = 2 * 1024 * 1024
DEFAULT_THRESHOLD = 3

# Deliberately simple keyword matching, not NLP: good enough to spot real repeats, and a person checks before acting on one.
DEFAULT_CATEGORIES = {
    "race/timing": ["race", "timing", "before the", "hadn't finished", "too fast", "too soon", "out of order"],
    "overflow/clipping": ["overflow", "clipped", "cut off", "truncat", "off by one", "off-by-one"],
    "wrong assumption/guessed": ["assumed", "guessed", "turned out to be wrong", "wrongly", "flawed reasoning", "never checked"],
    "self-reference/duplication": ["recursion", "duplicate", "itself", "self-inflicted"],
    "stale data/cache": ["stale", "cached", "out of date", "hadn't refreshed"],
    "quoting/encoding/paths": ["encoding", "utf-8", "quoting", "escaping", "backslash", "mangled"],
    "missing check/validation": ["no check", "missing check", "unvalidated", "didn't verify", "not verified", "silently"],
}


def get_root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def get_logs():
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()


def get_vault():
    return Path(os.environ.get("HARNESS_VAULT") or get_root() / "vault").resolve()


def load_categories():
    """-> {bucket: [keywords]}: logs/bug_categories.json when it holds a usable object, else the defaults."""
    try:
        data = json.loads((get_logs() / CATEGORIES_NAME).read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            cats = {str(k): [str(w) for w in v if str(w).strip()] for k, v in data.items() if isinstance(v, list)}
            if cats:
                return cats
    except (OSError, ValueError):
        pass
    return DEFAULT_CATEGORIES


def categorize(text, categories=None):
    """-> the first bucket with a keyword that starts a word in the text, else 'uncategorized'."""
    low = (text or "").lower()
    for bucket, words in (categories or load_categories()).items():
        if any(re.search(r"(?<![a-z0-9])" + re.escape(w.lower()), low) for w in words):
            return bucket
    return "uncategorized"


def daily_dir(notes=None):
    return Path(notes) if notes else get_vault() / DAILY_FOLDER


def find_bug_mentions(since=None, marker_re=MARKER_RE, notes=None, categories=None):
    """-> [{"date", "text", "category"}] for every bullet of every daily note (YYYY-MM-DD.md, any depth) that matches the marker, oldest first."""
    base = daily_dir(notes)
    cats = categories or load_categories()
    files = []
    if base.is_dir():
        for folder, _dirs, names in os.walk(base):
            files += [Path(folder) / n for n in names if DAILY_NAME_RE.match(n)]
    findings = []
    for path in sorted(files, key=lambda p: (p.stem, str(p))):
        date_str = path.stem
        if since and date_str < since:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read(MAX_NOTE_BYTES)
        except OSError:
            continue
        for bullet in BULLET_RE.findall(text):
            if marker_re.search(bullet):
                findings.append({"date": date_str, "text": bullet.strip(), "category": categorize(bullet, cats)})
    return findings


def retro_counts(findings):
    """-> [(bucket, count)] most common first."""
    return collections.Counter(f["category"] for f in findings).most_common()


def retro_text(findings, near_misses, threshold):
    lines = ["=== Retro: %d real bugs, %d near-misses ===" % (len(findings), len(near_misses)), "", "By category:"]
    for cat, n in retro_counts(findings):
        lines.append("  %s: %d%s" % (cat, n, ("  <- %d+, worth a standing pre-flight check" % threshold) if n >= threshold and cat != "uncategorized" else ""))
    return "\n".join(lines)


def list_text(findings):
    lines = ["=== %d 'real bug' mention(s) found - candidates for a standing-rule review ===" % len(findings), ""]
    for f in findings:
        preview = f["text"][:200] + ("..." if len(f["text"]) > 200 else "")
        lines.append("[%s] (%s) %s\n" % (f["date"], f["category"], preview))
    return "\n".join(lines)


def write_report(text):
    target = get_logs() / REPORT_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, target)


def raise_repeats(findings, threshold):
    """One alert and one work item for each bucket (other than uncategorized) at the threshold. The same bucket at the same count is raised once. Never raises."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import notify
        for cat, n in retro_counts(findings):
            if n >= threshold and cat != "uncategorized":
                notify.alert("Bug bucket '%s' came up %d times in the daily notes. Look at them and decide whether it needs a standing rule or a pre-flight check (report: logs/%s)." % (cat, n, REPORT_NAME),
                             source="bug_pattern_scanner", intent_kind="bug_pattern", tier=1, value=2, dedupe="bug-pattern-%s-%d" % (re.sub(r"\W+", "-", cat), n), logs=get_logs())
    except Exception:
        pass


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="bug_pattern_scanner.py", description="retro over the daily notes' real-bug bullets")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD")
    ap.add_argument("--retro", action="store_true")
    ap.add_argument("--job", action="store_true", help="scheduler mode: retro, save the report, alert on repeats, always exit 0")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument("--notes", default=None, help="folder of daily notes")
    a = ap.parse_args(argv)
    if a.since and not DATE_RE.match(a.since):
        print("--since must look like 2026-01-31")
        return 2
    if a.threshold < 1:
        print("--threshold must be 1 or more")
        return 2
    if a.job and (get_logs() / OFF_NAME).exists():
        return 0
    base = daily_dir(a.notes)
    if not base.is_dir():
        print("no daily notes folder at %s" % base)
        return 0 if a.job else 2
    findings = find_bug_mentions(a.since, notes=a.notes)
    if a.retro or a.job:
        text = retro_text(findings, find_bug_mentions(a.since, NEAR_MISS_RE, notes=a.notes), a.threshold)
        print(text)
        if a.job:
            try:
                write_report(text)
            except OSError:
                pass
            raise_repeats(findings, a.threshold)
        return 0
    print(list_text(findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
