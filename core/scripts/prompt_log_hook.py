# -*- coding: utf-8 -*-
"""UserPromptSubmit hook: keeps a small local log of what the user typed, so the learn step can see how they reacted to finished work.

Why: learn_outcomes.py and scoreboard.py need one signal the other scripts do not have, "did the user correct me or push me right after I finished something?".
This hook is that signal's only source. It writes one JSON line per prompt to logs/user_prompts.jsonl:
    {"ts": <epoch seconds>, "text": "<the prompt, redacted and cut to 600 characters>"}
Nothing leaves the computer and nothing reads the file except those two scripts. Obvious secrets are removed before anything is written: a word like password, token or
secret together with what follows it, well-known key formats, and any unbroken run of 32 or more key-like characters become "[redacted]". The log is trimmed to its newest
rows once it passes 1 MB.

Kill switch: create logs/prompt_log_off.txt and the hook records nothing (delete the file to switch it on again; delete logs/user_prompts.jsonl to forget what was recorded).
The hook prints nothing, always exits 0 and never breaks a prompt. It reads the hook JSON from stdin as UTF-8 bytes (field: prompt).

Pure functions (unit tested): redact(text), make_row(payload, now), read_prompts(logs, since), trim_rows(rows, keep).
Usage (hook command): python prompt_log_hook.py
"""
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

LOG_NAME = "user_prompts.jsonl"
OFF_NAME = "prompt_log_off.txt"
TEXT_CAP = 600                 # characters kept per prompt
MAX_BYTES = 1_000_000          # the log is trimmed above this size
KEEP_ROWS = 1000               # rows kept when it is trimmed (1000 rows of the longest allowed size are still under MAX_BYTES)
READ_CAP = 2_000_000           # never read more than this from the end of the log
STDIN_CAP = 4_000_000          # never parse more hook input than this

REDACT = [
    re.compile(r"(?i)\b(?:password|passwd|passphrase|pwd|secret|token|api[_ -]?key)\b\s*(?:is|=|:)?\s*\S+"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,})"),
    re.compile(r"[A-Za-z0-9_\-+/=]{32,}"),
]


def get_root():
    return Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()


def get_logs():
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()


def redact(text):
    """Pure. The text with secret-looking parts replaced by [redacted]."""
    out = str(text or "")
    for rx in REDACT:
        out = rx.sub("[redacted]", out)
    return out


def make_row(payload, now=None):
    """Pure. -> the row to log for a hook payload, or None when there is nothing to log (no prompt, or only white space)."""
    if not isinstance(payload, dict):
        return None
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    text = " ".join(redact(prompt[:5000]).split())[:TEXT_CAP]
    return {"ts": round(time.time() if now is None else float(now), 3), "text": text}


def trim_rows(rows, keep=None):
    """Pure. The newest `keep` rows (KEEP_ROWS when not given)."""
    keep = KEEP_ROWS if keep is None else keep
    return rows[-keep:] if keep > 0 else []


def _tail_text(path, cap=READ_CAP):
    """The last `cap` bytes of a file as text (the first, probably cut, line is dropped). '' when unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - cap))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > cap:
        text = text.split("\n", 1)[-1]
    return text


def read_prompts(logs=None, since=None):
    """-> [{"ts": float, "text": str}] oldest first, from the log. `since` (epoch) keeps only newer rows. [] when the log is missing or unreadable."""
    lg = Path(logs) if logs else get_logs()
    out = []
    for line in _tail_text(lg / LOG_NAME).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or isinstance(row.get("ts"), bool) or not isinstance(row.get("ts"), (int, float)):
            continue
        if since is not None and row["ts"] < since:
            continue
        out.append({"ts": float(row["ts"]), "text": str(row.get("text", ""))})
    return out


def _rewrite(path, rows):
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def record(payload, logs=None, now=None):
    """Append the row for this payload. -> True when a row was written. Does nothing while logs/prompt_log_off.txt exists. Never raises."""
    try:
        lg = Path(logs) if logs else get_logs()
        if (lg / OFF_NAME).exists():
            return False
        row = make_row(payload, now)
        if row is None:
            return False
        lg.mkdir(parents=True, exist_ok=True)
        path = lg / LOG_NAME
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if path.stat().st_size > MAX_BYTES:
            _rewrite(path, trim_rows(read_prompts(lg)))
        return True
    except Exception:
        return False


def read_stdin():
    """The hook JSON from stdin, read as UTF-8 bytes. {} on any problem."""
    try:
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read(STDIN_CAP) if stream is not None else sys.stdin.read(STDIN_CAP)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        raw = raw.lstrip("﻿")
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def main():
    try:
        record(read_stdin())
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    main()
    sys.exit(0)
