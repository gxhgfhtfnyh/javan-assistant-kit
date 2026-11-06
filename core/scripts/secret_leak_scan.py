# -*- coding: utf-8 -*-
"""Secret scan: looks through the notes and scripts for text that looks like a password, key or token that ended up in a plain file.

Scans ROOT/vault and ROOT/scripts (not logs, caches, version history or binary files, not files over 2 MB, and not the test files and detectors that hold fake keys on purpose). It looks for:
known key formats (sk-..., ghp_..., xox..., AKIA..., AIza...), private key blocks, password / token / secret / api key assignments with a long random-looking value, and .env-style lines
(NAME=long-random-value). Hashes (hex only) and ordinary words are not secrets. A finding becomes ONE intent (kind secret_found, tier 2 = ask the owner, value 5) per file, line and kind, plus a line
in logs/alerts.txt. The secret itself is never written anywhere: not in the intent, the alert, the screen or any log, only the place and the kind.
    python secret_leak_scan.py             # scan, report, raise intents; exit 1 when something was found
    python secret_leak_scan.py --dry-run   # scan and report only, write nothing
Pure core (unit tested): scan_text, looks_like_secret, should_scan, scan_tree."""
import argparse
import math
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import intent_store  # noqa: E402

EXCLUDE_DIRS = {".git", "__pycache__", "logs", "node_modules", ".venv", "venv", "dist", "build", ".obsidian", ".pytest_cache"}
SKIP_NAMES = {"secret_leak_scan.py", "leak_scan.py", "memory_write.py", "send_gate.py"}      # these hold the patterns themselves
MAX_BYTES = 2 * 1024 * 1024

KNOWN_KEY = re.compile(r"(?<![\w-])(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{30,}|(?:AKIA|ASIA)[A-Z0-9]{16})(?![\w-])")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")
ASSIGNMENT = re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|access[_-]?token|auth[_-]?token|bearer[_-]?token|client[_-]?secret|refresh[_-]?token)\b\s*[:=]\s*[\"']?([A-Za-z0-9_\-/.+=]{16,})[\"']?")
ENV_LINE = re.compile(r"(?m)^[A-Z][A-Z0-9_]*\s*=\s*[\"']?([A-Za-z0-9_\-/.+=]{24,})[\"']?\s*$")


def entropy(text):
    counts = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((n / len(text)) * math.log2(n / len(text)) for n in counts.values()) if text else 0.0


def looks_like_secret(value):
    """Random-looking, not a hash and not a plain word: at least 3 digits, more than hex characters, entropy of 3.0 bits or more."""
    if re.fullmatch(r"[0-9a-fA-F]+", value):
        return False
    return sum(c.isdigit() for c in value) >= 3 and entropy(value) >= 3.0


def scan_text(text):
    """-> sorted list of (line_no, kind, length). Never returns the secret itself."""
    found = set()
    for kind, rx, group in (("known_key", KNOWN_KEY, 0), ("private_key", PRIVATE_KEY, 0), ("assignment", ASSIGNMENT, 1), ("env_line", ENV_LINE, 1)):
        for m in rx.finditer(text):
            value = m.group(group)
            if kind in ("assignment", "env_line") and not looks_like_secret(value):
                continue
            found.add((text.count("\n", 0, m.start()) + 1, kind, len(value)))
    return sorted(found)


def should_scan(path):
    parts = set(path.replace("\\", "/").split("/"))
    name = os.path.basename(path)
    if parts & EXCLUDE_DIRS or name in SKIP_NAMES or (name.startswith("test_") and name.endswith(".py")):
        return False
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return False
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    return b"\x00" not in head


def scan_tree(root, folders=("vault", "scripts")):
    """-> list of (relative path, line, kind, length)."""
    out = []
    for folder in folders:
        base = os.path.join(root, folder)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                if not should_scan(path):
                    continue
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                except OSError:
                    continue
                rel = os.path.relpath(path, root).replace("\\", "/")
                out.extend((rel, line, kind, length) for line, kind, length in scan_text(text))
    return out


def root():
    return os.environ.get("HARNESS_ROOT") or os.path.dirname(HERE)


def logs():
    return os.environ.get("HARNESS_LOGS") or os.path.join(root(), "logs")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Look for secrets that ended up in plain files.")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--job", action="store_true", help="scheduled run: raise the items and alerts but always exit 0 (a nonzero exit would read as a broken job)")
    args = ap.parse_args(argv)
    findings = scan_tree(root())
    lg = logs()
    if not args.dry_run:
        os.makedirs(lg, exist_ok=True)
        with open(os.path.join(lg, "sensor_last.txt"), "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    if not findings:
        print("no likely secrets found")
        return 0
    intents = intent_store.read_jsonl(intent_store.INTENTS, lg)
    added = False
    for rel, line, kind, length in findings:
        cand = intent_store.new_intent("secret_found", "possible secret in %s:%d (%s)" % (rel, line, kind), tier=2, value=5, dedupe="secret:%s:%d:%s" % (rel, line, kind))
        if not intent_store.is_duplicate(intents, cand) and not args.dry_run:
            intents.append(cand)
            added = True
            os.makedirs(lg, exist_ok=True)
            with open(os.path.join(lg, "alerts.txt"), "a", encoding="utf-8") as f:
                f.write("%s secret_found: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), cand["what"]))
    if added:
        intent_store.write_intents(intents, lg)
    print("%d possible secret(s) found (the values are not shown):" % len(findings))
    for rel, line, kind, length in findings:
        print("  %s:%d  %s, %d characters" % (rel, line, kind, length))
    return 0 if args.dry_run or args.job else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
