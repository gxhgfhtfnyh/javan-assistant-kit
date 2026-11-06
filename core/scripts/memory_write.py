# -*- coding: utf-8 -*-
"""Memory write funnel: the one door durable facts go through.

Appends a dated bullet to a chosen note (frontmatter handling kept),
refuses to write outside the vault, refuses text that looks like a secret
(api key, password, token, private key patterns), flags a likely conflict
with an existing fact (the 3 nearest existing lines by word overlap that
differ in numbers or dates), and with --replace updates that line in place
instead of appending a stale duplicate. Appends one line per write to
logs/memory_writes.jsonl.

Usage:
    python memory_write.py "note.md" "fact text" [--replace] [--dry-run]
    python memory_write.py --note "note.md" --text "fact text" [--replace] [--dry-run]

Paths are relative to the vault (ROOT/vault, or HARNESS_VAULT env var).
Logs go to ROOT/logs/memory_writes.jsonl (or HARNESS_LOGS env var).
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

def get_root() -> Path:
    return Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()

def get_vault() -> Path:
    return Path(os.environ.get("HARNESS_VAULT") or get_root() / "vault").resolve()

def get_logs() -> Path:
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()

# Secret patterns to reject
SECRET_PATTERNS = [
    re.compile(r"\bapi[_-]?key\s*[:=]\s*[\w-]{16,}", re.I),
    re.compile(r"\bpassword\s*[:=]\s*\S{8,}", re.I),
    re.compile(r"\btoken\s*[:=]\s*[\w.-]{16,}", re.I),
    re.compile(r"\bsecret\s*[:=]\s*\S{16,}", re.I),
    re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----", re.I),   # RSA, EC, DSA, OPENSSH, PGP and the bare PKCS#8 form
    re.compile(r"sk-[\w-]{32,}", re.I),  # OpenAI-style keys
    re.compile(r"gh[ps]_[A-Za-z0-9_]{32}", re.I),  # GitHub tokens (36 total including ghp_)
    re.compile(r"xox[abp]-[\w-]{20,}", re.I),  # Slack tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key ids
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),  # Google API keys
]

# the "- 2026-10-31 10:20 " stamp this funnel puts in front of every bullet; it must not count as part of the fact when facts are compared
BULLET_STAMP = re.compile(r"^\s*[-*]\s*\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2})?\s*")

# Words to ignore for overlap scoring
STOP_WORDS = frozenset("""
the a an and or of to in on at for is are was were do does did what which when
who how many much that this with from it its be by as pick give has have had
but not you your our their his her its my me we us them they he she it
""".split())


def normalize_path(path: str, base: Path) -> Path:
    """Resolve path relative to base, ensuring it's inside base."""
    p = (base / path).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        raise ValueError(f"Path {path!r} escapes the vault")
    return p


def read_note_lines(note_path: Path) -> List[str]:
    """Read note, return lines preserving frontmatter."""
    if not note_path.exists():
        return []
    return note_path.read_text(encoding="utf-8").splitlines()


def write_note_lines(note_path: Path, lines: List[str]) -> None:
    """Write lines to note, ensuring trailing newline."""
    note_path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    tmp = note_path.with_name(note_path.name + ".tmp")        # write beside it, then swap: a crash mid-write must not leave a half note
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, note_path)


def split_frontmatter(lines: List[str]) -> Tuple[List[str], List[str]]:
    """Split lines into frontmatter and body. Returns (frontmatter, body)."""
    if not lines or not lines[0].strip() == "---":
        return [], lines
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return lines[:i+1], lines[i+1:]
    return [], lines  # No closing ---


def detect_secrets(text: str) -> List[str]:
    """Return list of secret pattern names found in text."""
    found = []
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            found.append(pattern.pattern[:50])
    return found


def score_overlap(query: str, target: str) -> int:
    """Score word overlap between query and target (excluding stop words)."""
    q_words = set(w.lower() for w in re.findall(r"\w+", query) if w.lower() not in STOP_WORDS)
    t_words = set(w.lower() for w in re.findall(r"\w+", target) if w.lower() not in STOP_WORDS)
    if not q_words or not t_words:
        return 0
    return len(q_words & t_words)


def differs_in_numbers_or_dates(query: str, target: str) -> bool:
    """Check if query and target differ in numbers or dates."""
    q_nums = re.findall(r"\b\d+(?:\.\d+)?\b", query)
    t_nums = re.findall(r"\b\d+(?:\.\d+)?\b", target)
    q_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b", query)
    t_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b", target)
    return q_nums != t_nums or q_dates != t_dates


def find_conflicts(note_path: Path, new_text: str, top_n: int = 3) -> List[Tuple[int, str, int]]:
    """Find potential conflicts in existing note lines.

    Returns list of (line_no, line_text, overlap_score) for top N lines
    that have high word overlap but differ in numbers/dates.
    """
    lines = read_note_lines(note_path)
    _, body = split_frontmatter(lines)
    conflicts = []
    for i, line in enumerate(body):
        fact = BULLET_STAMP.sub("", line)
        score = score_overlap(new_text, fact)
        if score >= 2 and differs_in_numbers_or_dates(new_text, fact):
            # line_no is 1-indexed including frontmatter
            fm, _ = split_frontmatter(lines)
            line_no = len(fm) + i + 1
            conflicts.append((line_no, line.strip(), score))
    conflicts.sort(key=lambda x: -x[2])
    return conflicts[:top_n]


def format_bullet(text: str, now: Optional[float] = None) -> str:
    """Format a dated bullet line."""
    t = time.localtime(now or time.time())
    return f"- {time.strftime('%Y-%m-%d %H:%M', t)} {text.strip()}"


def append_to_note(note_path: Path, text: str, replace_line: Optional[int] = None, dry_run: bool = False) -> Tuple[str, List[Tuple[int, str, int]]]:
    """Append a bullet to note, or replace a line if replace_line is given.

    Returns (status_message, conflicts).
    """
    conflicts = find_conflicts(note_path, text)
    lines = read_note_lines(note_path)
    fm, body = split_frontmatter(lines)
    bullet = format_bullet(text)

    if replace_line is not None:
        if replace_line <= len(fm) or replace_line > len(lines):
            raise ValueError(f"Line {replace_line} is not in the note body")
        lines[replace_line - 1] = bullet
        action = f"replaced line {replace_line}"
    else:
        # Append after frontmatter (at end of body)
        lines.append(bullet)
        action = "appended"

    if not dry_run:
        write_note_lines(note_path, lines)

    return action, conflicts


def log_write(note_rel: str, text: str, action: str, conflicts: List[Tuple[int, str, int]], dry_run: bool) -> None:
    """Append a line to memory_writes.jsonl."""
    logs = get_logs()
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / "memory_writes.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": note_rel,
        "text": text,
        "action": action,
        "conflicts": [{"line": c[0], "text": c[1], "score": c[2]} for c in conflicts],
        "dry_run": dry_run,
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_fact(note: str, text: str, replace: bool = False, dry_run: bool = False, source: str = "session") -> Dict[str, Any]:
    """Main write function. Returns dict with status and details. `source` says where the fact came from: a trusted source (session, self, vault, script, test) goes into the note
    unless it reads like an instruction aimed at an AI; anything else (a web page, a friend's message, an email, an unknown source) is held in quarantine for the owner and never reaches the note."""
    vault = get_vault()
    text = " ".join(str(text).split())            # one bullet is one line: newlines and runs of spaces would break the layout and the in-place replace
    if not text:
        return {"status": "refused", "reason": "empty text"}
    # Validate paths
    try:
        note_path = normalize_path(note, vault)
    except ValueError as e:
        return {"status": "refused", "reason": str(e)}

    # Check for secrets
    secrets = detect_secrets(text)
    if secrets:
        return {"status": "refused", "reason": f"Text contains secret patterns: {secrets}"}

    import quarantine                                  # the fence between outside content and memory
    if quarantine.route(text, source) == "quarantine":
        if dry_run:
            return {"status": "dry-run", "note": None, "action": "would be held in quarantine", "conflicts": []}
        row = quarantine.hold(text, source, logs=str(get_logs()))
        return {"status": "quarantined", "id": row["id"], "reason": "outside or instruction-like text is held for the owner and was not written to the note"}

    try:
        # Check conflicts
        conflicts = find_conflicts(note_path, text)

        # Determine replace_line
        replace_line = None
        if replace and conflicts:
            replace_line = conflicts[0][0]

        # Perform write
        action, conflicts = append_to_note(note_path, text, replace_line, dry_run)
    except (ValueError, OSError) as e:             # UnicodeDecodeError is a ValueError: a note that is not UTF-8 is reported, not crashed on
        return {"status": "error", "reason": str(e)}

    # Log
    note_rel = str(note_path.relative_to(vault))
    log_write(note_rel, text, action, conflicts, dry_run)

    status = "dry-run" if dry_run else "wrote"
    return {
        "status": status,
        "note": note_rel,
        "action": action,
        "conflicts": [{"line": c[0], "text": c[1], "score": c[2]} for c in conflicts],
    }


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    # Parse args
    note = None
    text = None
    replace = False
    dry_run = False
    source = "session"

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--source" and i + 1 < len(argv):
            source = argv[i + 1]
            i += 1
        elif arg == "--replace":
            replace = True
        elif arg == "--dry-run":
            dry_run = True
        elif arg == "--note" and i + 1 < len(argv):
            note = argv[i + 1]
            i += 1
        elif arg == "--text" and i + 1 < len(argv):
            text = argv[i + 1]
            i += 1
        elif not arg.startswith("-"):
            if note is None:
                note = arg
            elif text is None:
                text = arg
        i += 1

    if not note or not text:
        print("Usage: memory_write.py <note> <text> [--replace] [--dry-run] [--source NAME]", file=sys.stderr)
        print("       memory_write.py --note <note> --text <text> [--replace] [--dry-run]", file=sys.stderr)
        return 2

    result = write_fact(note, text, replace, dry_run, source)
    print(json.dumps(result))              # ASCII-escaped: a narrow console or pipe encoding must not crash the report after the note was written
    return 0 if result["status"] in ("wrote", "dry-run", "quarantined") else 1


if __name__ == "__main__":
    sys.exit(main())