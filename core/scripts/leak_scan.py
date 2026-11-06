# -*- coding: utf-8 -*-
"""Leak scan: finds personal data and secrets left in a folder before it is shared (your assistant folder, a backup, a zip).

Two kinds of finding:
  generic   patterns that are personal or secret wherever they appear: e-mail addresses (except example.com/org/net and noreply), 17-20 digit chat ids,
            Windows user-profile paths, private IPv4 addresses, MAC addresses, secret-looking assignments and well-known key formats.
  denylist  terms from a private file that is NEVER inside the kit (the names, ids, school codes and paths that belong to this PC). One term per line, '#' comments:
              plain text    case-insensitive substring
              w:Zebra        case-insensitive whole word
              cs:Quokka       case-sensitive whole word
              re:<regex>    a regular expression (case-insensitive)
    python leak_scan.py <folder> [--denylist <file>]        # prints 'path:line: kind: matched text', exits 1 when anything was found
Binary files, __pycache__ and .pytest_cache are skipped. Only the matched text is printed, never the whole line, so a secret is not copied into the report.
Pure core (unit tested): parse_denylist, scan_text, scan_tree."""
import argparse
import os
import re
import sys

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", "node_modules"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pyc", ".zip", ".gz", ".7z", ".pdf", ".mov", ".mp4", ".npz", ".db", ".sqlite", ".exe", ".dll"}
MAX_BYTES = 5 * 1024 * 1024
OK_EMAIL = re.compile(r"@(?:example\.(?:com|org|net)|users\.noreply\.github\.com|localhost)\b|no-?reply", re.I)

GENERIC = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("chat_id", re.compile(r"(?<![\d.])\d{17,20}(?![\d.])")),
    ("user_path", re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?![<%{$]|Public\b|Default\b|All Users\b|you\b|name\b|USERNAME\b)[^\\/\s\"'`<>]+", re.I)),
    ("private_ip", re.compile(r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?![\d.])")),
    ("mac_address", re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])")),
    ("secret_assignment", re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*['\"][A-Za-z0-9_\-./+=]{16,}['\"]")),
    ("key_format", re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{20,}|ghp_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,})")),
]


def parse_denylist(text):
    """-> list of (label, compiled regex). Blank lines and '#' comments are ignored; a bad regex line raises ValueError naming the line."""
    out = []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if line.startswith("re:"):
                rx = re.compile(line[3:], re.I)
            elif line.startswith("w:"):
                rx = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(line[2:]) + r"(?![A-Za-z0-9_])", re.I)
            elif line.startswith("cs:"):
                rx = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(line[3:]) + r"(?![A-Za-z0-9_])")
            else:
                rx = re.compile(re.escape(line), re.I)
        except re.error as e:
            raise ValueError("denylist line %d is not a valid pattern: %s" % (n, e))
        out.append((line, rx))
    return out


def scan_text(text, deny=(), generic=True):
    """-> list of (line_no, kind, matched text) for one file's text. generic=False checks only the denylist (for test files that hold fake keys on purpose)."""
    found = []
    for n, line in enumerate((text or "").splitlines(), 1):
        for kind, rx in (GENERIC if generic else ()):
            for m in rx.finditer(line):
                if kind == "email" and OK_EMAIL.search(m.group(0)):
                    continue
                found.append((n, kind, m.group(0)))
        for label, rx in deny:
            for m in rx.finditer(line):
                found.append((n, "denylist:" + label, m.group(0)))
    return found


def scan_tree(root, deny=(), ignore=(), generic=True):
    """-> list of (relative path, line_no, kind, matched text) for every text file under root (the file names are scanned too).
    `ignore` is a collection of relative paths (forward slashes) of files that hold planted fake data on purpose, such as this scanner's own tests."""
    findings = []
    skipped = {i.replace("\\", "/").lstrip("./") for i in ignore}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            if rel.replace("\\", "/") in skipped:
                continue
            for _n, kind, hit in scan_text(rel.replace("\\", "/"), deny, generic):
                findings.append((rel, 0, kind + " (in the file name)", hit))
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            try:
                if os.path.getsize(path) > MAX_BYTES:
                    findings.append((rel, 0, "skipped", "file over %d MB not scanned" % (MAX_BYTES // (1024 * 1024))))
                    continue
                with open(path, "rb") as f:
                    raw = f.read()
            except OSError as e:
                findings.append((rel, 0, "unreadable", str(e)))
                continue
            if b"\x00" in raw[:4096]:
                continue  # binary
            for n, kind, hit in scan_text(raw.decode("utf-8", errors="replace"), deny, generic):
                findings.append((rel, n, kind, hit))
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan a folder for personal data before the starter kit is shared.")
    ap.add_argument("folder")
    ap.add_argument("--denylist", help="private file of terms that belong to this PC (never inside the kit)")
    ap.add_argument("--ignore", action="append", default=[], metavar="RELPATH", help="a file under the folder that holds planted fake data on purpose (repeatable)")
    ap.add_argument("--only-denylist", action="store_true", help="skip the generic patterns and check only the denylist terms")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.folder):
        print("not a folder: " + args.folder)
        return 2
    deny = []
    if args.denylist:
        try:
            with open(args.denylist, encoding="utf-8") as f:
                deny = parse_denylist(f.read())
        except (OSError, ValueError) as e:
            print("denylist problem: %s" % e)
            return 2
    findings = scan_tree(args.folder, deny, args.ignore, generic=not args.only_denylist)
    for rel, n, kind, hit in findings:
        print("%s:%s: %s: %s" % (rel, n, kind, hit))
    print("%d finding(s) in %s (%d denylist term(s)%s)" % (len(findings), args.folder, len(deny), ", ignoring " + ", ".join(args.ignore) if args.ignore else ""))
    return 1 if findings else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
