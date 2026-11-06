# -*- coding: utf-8 -*-
"""UserPromptSubmit hook: recall without being asked.

Why: an assistant often answers from the conversation instead of checking its memory. This hook looks at each
prompt, and when it reads like a question about something the vault may hold (a question, a script name,
"last time", "we decided"), it searches the memory and prints the top lines under one [RECALL] heading, capped
at about 1500 characters (roughly 400 tokens), so the answer is already in front of the assistant when it starts.

How the search works: memory_search.py (same folder) scores every line of every .md note under the vault with
BM25 plus a rare-word boost, in memory, on each call. There is no index file to build. "No index" therefore means
the vault folder is missing or holds no .md notes; in that case the hook prints nothing and never builds anything.

Gate rules: automated or system messages are skipped; prompts under 20 characters and slash commands are skipped;
a search that takes over 4 seconds is abandoned; any error prints nothing (a hook must never break a prompt).
Kill switch: create logs/recall_off.txt. Every run that searched is logged to logs/recall_log.jsonl
(ts, q first 120 chars, ms, lines, chars).

    python recall_hook.py --test "which script syncs the backups?"   # run the whole path and print what would be injected

As a hook it reads the Claude Code JSON from stdin (field: prompt) as UTF-8 bytes and prints the block to stdout.
Pure core (unit tested): extract_query(prompt), should_recall(query), format_block(lines, max_chars).
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs),
HARNESS_VAULT (default ROOT/vault).
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()
SCRIPTS = Path(__file__).parent.resolve()
SEARCH_SCRIPT = SCRIPTS / "memory_search.py"

OFF_NAME = "recall_off.txt"
LOG_NAME = "recall_log.jsonl"
MAX_CHARS = 1500            # about 375 tokens
LINE_CHARS = 230
QUERY_CHARS = 400
TOP = 5
TIMEOUT = 4                 # seconds; a slower search is abandoned

HEADER = "[RECALL] vault lines that may answer this (index search, check before relying on them):"
QUESTION_START = re.compile(
    r"^\W*(what|which|who|whom|whose|how|when|where|why|is|are|was|were|does|do|did|can|could|should|would|will|has|have|any)\b",
    re.I)
TRIGGERS = re.compile(
    r"\b\w+\.py\b|last time|earlier|before|we decided|remember|did we|have we|what happened|where is|where did|which one|\bagain\b",
    re.I)
AUTOMATED = re.compile(
    r"^\s*(\[SYSTEM NOTIFICATION|<task-notification|Work tick|\[Request interrupted|<system-reminder|\[no-reply\])",
    re.I)


# ---------------------------------------------------------------- pure core

def extract_query(prompt):
    """Pure. The text worth searching for: the prompt on one line, first 400 characters.
    Automated or system messages (notifications, ticks, interrupts, reminders) give ""."""
    p = prompt if isinstance(prompt, str) else ""
    if AUTOMATED.match(p):
        return ""
    return " ".join(p.split())[:QUERY_CHARS]


def should_recall(query):
    """Pure. Whether the message looks like it asks about something the vault may hold."""
    q = (query if isinstance(query, str) else "").strip()
    if len(q) < 20 or q.startswith("/"):
        return False
    return "?" in q or bool(QUESTION_START.match(q)) or bool(TRIGGERS.search(q))


def format_block(lines, max_chars=MAX_CHARS):
    """Pure. -> the injected text, or "" when there is nothing.
    Each input line is 'note path | score | text' (what memory_search.py prints); the note's folder and .md are
    dropped to save tokens, each text is cut at 230 characters, and the whole block stays within max_chars."""
    out, used = [], 0
    for ln in lines or []:
        if not isinstance(ln, str):
            continue
        parts = [x.strip() for x in ln.split(" | ", 2)]
        if len(parts) < 3 or not parts[2]:
            continue
        note = os.path.basename(parts[0].replace("\\", "/"))
        note = note[:-3] if note.endswith(".md") else note
        item = "- %s: %s" % (note, parts[2][:LINE_CHARS])
        if used + len(item) > max_chars:
            break
        out.append(item)
        used += len(item)
    return (HEADER + "\n" + "\n".join(out)) if out else ""


# ---------------------------------------------------------------- search and logging

def has_index(vault):
    """True when there is something to search: the vault folder exists and holds at least one .md note.
    memory_search.py builds its scoring in memory per call, so there is no file to build here."""
    try:
        v = Path(vault)
        if not v.is_dir():
            return False
        for p in v.rglob("*.md"):
            if ".obsidian" in p.parts or ".git" in p.parts:
                continue
            return True
        return False
    except Exception:
        return False


def search_vault(query, vault=None):
    """Run memory_search.py on the query -> its output lines ('note | score | text'). [] on any failure or timeout."""
    q = (query or "").lstrip("-").strip()             # the search CLI drops arguments that start with a dash
    if not q:
        return []
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", HARNESS_VAULT=str(vault or VAULT))
    kwargs = {}
    if sys.platform == "win32":                       # no console window flashing for the child on Windows
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(
            [sys.executable, str(SEARCH_SCRIPT), q, "--top", str(TOP)],
            cwd=str(SCRIPTS), capture_output=True, text=True, timeout=TIMEOUT,
            encoding="utf-8", errors="replace", env=env, **kwargs)
    except Exception:                                 # timeout, missing interpreter, anything
        return []
    if r.returncode != 0:
        return []
    return (r.stdout or "").splitlines()


def write_log(logs, q, ms, nlines, nchars):
    """Append one line to logs/recall_log.jsonl. Never raises."""
    try:
        logs = Path(logs)
        logs.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "q": q[:120], "ms": ms, "lines": nlines, "chars": nchars}
        with (logs / LOG_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def run(prompt, logs=None, search=None, vault=None):
    """-> the text to print ("" for nothing). Never raises.
    `search` is an optional function query -> list of 'note | score | text' lines (tests inject a fake one)."""
    try:
        logs = Path(logs) if logs else LOGS
        if (logs / OFF_NAME).exists():
            return ""
        q = extract_query(prompt)
        if not should_recall(q):
            return ""
        vault = Path(vault) if vault else VAULT
        if not has_index(vault):
            return ""
        fn = search if search is not None else (lambda text: search_vault(text, vault))
        t0 = time.time()
        try:
            lines = list(fn(q) or [])
        except Exception:
            lines = []
        ms = int((time.time() - t0) * 1000)
        block = format_block(lines)
        write_log(logs, q, ms, len(lines), len(block))
        return block
    except Exception:
        return ""


# ---------------------------------------------------------------- entry point

def read_payload():
    """Read the hook JSON from stdin as UTF-8 bytes. {} on any problem."""
    try:
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig", errors="replace")
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def emit(text):
    """Print as UTF-8 even when the console code page is narrow."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(text)


def main(argv=None):
    try:
        argv = list(argv if argv is not None else sys.argv[1:])
        if argv[:1] == ["--test"]:
            emit(run(" ".join(argv[1:])) or "(nothing injected)")
            return 0
        out = run(read_payload().get("prompt", ""))
        if out:
            emit(out)
    except Exception:
        pass                                          # never break the prompt
    return 0


if __name__ == "__main__":
    sys.exit(main())
