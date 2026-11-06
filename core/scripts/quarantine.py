# -*- coding: utf-8 -*-
"""Quarantine: keeps untrusted content from becoming memory.

Why: content from outside (web pages, other people's messages, email, transcripts,
tool output) is data, never instructions, but the memory system would otherwise
write down whatever a session happened to learn, including a sentence a stranger
planted. This is the fence: anything learned from an untrusted source goes to
logs/quarantine.jsonl with its source and a flag when it reads like an
instruction aimed at the AI. Only the owner or a clean session promotes a row,
and promotion records who did it. Nothing here writes to the vault itself:
`memory_write.py` (the one write funnel) asks `route()` first and obeys it.

    python quarantine.py hold --source friend_dm --text "..."      # hold a row, prints its id
    python quarantine.py list [--all]                               # held rows
    python quarantine.py promote <id>                                # mark one promoted; needs unexpired approval in approvals.jsonl
    python quarantine.py drop <id> --by assistant                   # mark one dropped

Pure core (unit tested): route(text, source), looks_like_instruction(text),
new_row(text, source, ts), promote_row(row, by, ts)."""
import hashlib
import json
import os
import re
import sys
import time


def _logs_dir():
    return os.environ.get("HARNESS_LOGS") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


FILE = "quarantine.jsonl"
APPROVALS_FILE = "approvals.jsonl"
TRUSTED = {"self", "vault", "session", "test", "script"}
UNTRUSTED_HINTS = ("friend", "dm", "group", "mail", "email", "gmail", "web", "page", "fetch", "transcript", "video", "tool_output", "unknown")
FALLBACK_PHRASES = [
    r"ignore (all )?(previous|prior|above) instructions",
    r"disregard (all )?(previous|prior|above)",
    r"new system prompt",
    r"\bsystem\s*:\s*you (are|must|will)",
    r"do not (tell|inform|mention this to) (the user|the owner|the assistant)",
    r"jailbreak",
    r"\b(you must|you should|you will) (now )?(send|delete|run|execute|forward|reply|ignore|remember|save)\b",
    r"\b(claude|assistant|ai)[, :]+(please )?(send|delete|run|execute|forward|ignore|remember|save)\b",
]
_CACHE = {}


def _rx():
    if "rx" in _CACHE:
        return _CACHE["rx"]
    _CACHE["rx"] = re.compile("|".join(FALLBACK_PHRASES), re.IGNORECASE)
    return _CACHE["rx"]


def looks_like_instruction(text):
    """Pure. True when the text reads like it is giving orders to an AI or trying to hide something from the owner."""
    return bool(_rx().search(text or ""))


def route(text, source):
    """Pure. -> "vault" for content from a trusted source that does not read as an instruction, otherwise "quarantine".
    An unknown source is untrusted: the safe default for a fence."""
    src = (source or "unknown").strip().lower()
    if src in TRUSTED and not looks_like_instruction(text):
        return "vault"
    return "quarantine"


def new_row(text, source, ts=None):
    ts = time.time() if ts is None else ts
    body = (text or "")[:2000]
    return {"id": hashlib.sha1(("%s|%s" % (source, body)).encode("utf-8", "replace")).hexdigest()[:12], "ts": ts, "source": source or "unknown",
            "flag": looks_like_instruction(body), "text": body, "status": "held", "by": None}


def promote_row(row, by, ts=None):
    """Pure. -> a copy marked promoted by `by`. An unflagged-looking row still needs a named promoter; no name, no promotion."""
    if not by or not str(by).strip():
        raise ValueError("promotion needs a name")
    out = dict(row)
    out.update(status="promoted", by=str(by).strip(), decided=time.time() if ts is None else ts)
    return out


def _read(logs=None):
    out = []
    try:
        with open(os.path.join(logs or _logs_dir(), FILE), encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def _write(rows, logs=None):
    p = os.path.join(logs or _logs_dir(), FILE)
    os.makedirs(os.path.dirname(p), exist_ok=True)          # the first thing ever held creates the logs folder
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, p)


def _read_approvals(logs=None):
    out = []
    try:
        with open(os.path.join(logs or _logs_dir(), APPROVALS_FILE), encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def _approval_sha(text):
    """Return sha256 hex of normalized text (strip, collapse whitespace)."""
    normalized = re.sub(r"\s+", " ", text.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _epoch(value):
    """An expiry as epoch seconds: a number, or the ISO text approve.py writes ("2026-11-01T10:00:00+00:00"). None when it is neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _has_valid_approval(row_id, logs=None, now=None):
    """Check if approvals.jsonl has an unexpired approval whose sha256 equals sha256('promote <id>')."""
    if now is None:
        now = time.time()
    target_sha = _approval_sha(f"promote {row_id}")
    for a in _read_approvals(logs):
        if a.get("sha") == target_sha:
            until = _epoch(a.get("expires"))
            if until is not None and until > now:                # no expiry, or one that cannot be read, never counts as an approval
                return True
    return False


def hold(text, source, logs=None, ts=None):
    """Append a held row (once per identical source+text) and return it."""
    rows = _read(logs)
    row = new_row(text, source, ts)
    for r in rows:
        if r["id"] == row["id"]:
            return r
    rows.append(row)
    _write(rows, logs)
    return row


def decide(row_id, by, status, logs=None, ts=None, check=True):
    """-> the updated row, None if there is no such row, or the string "no_approval" when a promotion has no valid approval (check=False is for tests and for dropping)."""
    rows = _read(logs)
    for i, r in enumerate(rows):
        if r["id"].startswith(row_id):
            if status == "promoted":
                if check and not _has_valid_approval(r["id"], logs, ts):
                    return "no_approval"
                rows[i] = dict(promote_row(r, by, ts))
            else:
                rows[i] = dict(r, status="dropped", by=by, decided=time.time() if ts is None else ts)
            _write(rows, logs)
            return rows[i]
    return None


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] not in ("hold", "list", "promote", "drop"):
        print(__doc__.split("Pure core")[0].strip())
        return 2

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default

    cmd = argv[0]
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if cmd == "hold":
        if not opt("--text", "").strip():
            print("nothing to hold: --text is empty")
            return 2
        r = hold(opt("--text", ""), opt("--source", "unknown"))
        print(r["id"], "flagged" if r["flag"] else "held")
        return 0
    if cmd == "list":
        for r in _read():
            if "--all" in argv or r["status"] == "held":
                print("%s %-9s %-10s %s%s" % (r["id"], r["status"], r["source"], "[FLAG] " if r["flag"] else "", r["text"][:80].replace("\n", " ")))
        return 0
    row = decide(argv[1] if len(argv) > 1 else "", opt("--by", "owner"), "promoted" if cmd == "promote" else "dropped", logs=_logs_dir())
    if row == "no_approval":
        print("no valid unexpired approval found for this promotion")
        return 3
    print("no such row" if row is None else "%s %s by %s" % (row["id"], row["status"], row["by"]))
    return 0 if row else 1


if __name__ == "__main__":
    sys.exit(main())