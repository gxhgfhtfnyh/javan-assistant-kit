# -*- coding: utf-8 -*-
"""PostToolUse hook on Write: when the assistant CREATES a new script or a new vault note, prints ONE line naming the closest existing ones, so it reuses or merges instead of duplicating.

Why: an assistant that builds a lot ends up writing the same tool twice, or a second note beside one that already says the same thing. Searching first is the
rule; this hook is the mechanical reminder at the moment it matters.
 - a new script in the scripts folder: docstring-vocabulary overlap (Jaccard) with every other script, at most 2 named
 - a new .md note in the vault: title-word overlap with the other note titles, and with script names and docstrings (2+ shared title words)
Only a CREATE counts: Claude Code reports tool_response.type "create" for a new file and "update" for an overwrite; when the response does not say, a file whose
creation time is under 3 seconds old counts as new. Calls made by a sub-agent are skipped. It prints a plain line (no JSON) wrapped as additional context.
Never blocks a tool, never raises, always exits 0. Kill switch: logs/similar_check_off.txt. Log: logs/similar_check.jsonl (rotated above 512 KB).

Hook config: PostToolUse, matcher "Write". Reads the hook JSON from stdin as UTF-8 bytes and prints
{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "..."}} when something similar exists.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault). Scripts folders: ROOT/scripts and this file's folder.
Pure core (unit tested): words, jaccard, similar_scripts, similar_notes, script_hits, is_new_file, handle."""
import ast
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OFF_NAME = "similar_check_off.txt"
LOG_NAME = "similar_check.jsonl"
SCRIPT_LIMIT = 0.35
NOTE_LIMIT = 0.5
MAX_DOC_BYTES = 512 * 1024            # a script longer than this is not parsed for its docstring
MAX_NOTES = 5000                      # titles collected from the vault: bounded time on a huge vault
LOG_ROTATE_BYTES = 512 * 1024
NEW_FILE_SECONDS = 3
STOP = set("the and for with from this that into your have are was will not but all can its our out new old one two use used using script scripts note notes file files python hook check tool tools".split())


def root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent)


def logs_dir():
    return Path(os.environ.get("HARNESS_LOGS") or root() / "logs")


def vault_dir():
    return Path(os.environ.get("HARNESS_VAULT") or root() / "vault")


def scripts_dirs():
    out = []
    for d in (root() / "scripts", HERE):
        if d not in out:
            out.append(d)
    return out


def words(text, minlen=4):
    return {w for w in re.findall(r"[a-z]{%d,}" % minlen, (text or "").lower()) if w not in STOP}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b and (a | b) else 0.0


def doc_words(path):
    """The vocabulary of a script's module docstring; an empty set when it cannot be read or parsed."""
    try:
        if os.path.getsize(path) > MAX_DOC_BYTES:
            return set()
        with open(path, "rb") as f:
            src = f.read().decode("utf-8-sig", errors="replace")
        return words(ast.get_docstring(ast.parse(src)) or "")
    except Exception:
        return set()


def _scripts_in(scripts_dir):
    try:
        return sorted(f for f in os.listdir(scripts_dir) if f.endswith(".py") and not f.startswith("test_"))
    except OSError:
        return []


def similar_scripts(new_path, scripts_dir, limit=SCRIPT_LIMIT):
    """[(score, file name)] for the scripts in scripts_dir whose docstring vocabulary overlaps the new script's, best first, at most 2."""
    mine = doc_words(new_path)
    if len(mine) < 8:
        return []
    out = []
    for f in _scripts_in(scripts_dir):
        p = os.path.join(scripts_dir, f)
        if os.path.normcase(os.path.abspath(p)) != os.path.normcase(os.path.abspath(new_path)):
            j = jaccard(mine, doc_words(p))
            if j >= limit:
                out.append((round(j, 2), f))
    return sorted(out, reverse=True)[:2]


def similar_notes(new_title, titles, limit=NOTE_LIMIT):
    """[(score, title)] for note titles that share most of their meaningful words with the new title; a title with under 2 meaningful words is not judged."""
    mine = words(new_title, 4)
    if len(mine) < 2:
        return []
    out = [(round(len(mine & words(t, 4)) / min(len(mine), len(words(t, 4)) or 1), 2), t) for t in titles if t != new_title]
    return sorted([o for o in out if o[0] >= limit], reverse=True)[:2]


def script_hits(new_title, scripts_dir, need=2):
    """[(shared word count, file name)] for scripts whose name or docstring holds at least `need` of the note title's words."""
    mine = words(new_title, 5)
    if len(mine) < need:
        return []
    out = []
    for f in _scripts_in(scripts_dir):
        hay = words(f.replace("_", " ") + " " + " ".join(doc_words(os.path.join(scripts_dir, f))), 5)
        n = len(mine & hay)
        if n >= need:
            out.append((n, f))
    return sorted(out, reverse=True)[:2]


def note_titles(vault):
    """The titles (file names without .md) of the notes in the vault, skipping hidden folders; at most MAX_NOTES."""
    titles = []
    try:
        for d, dirs, files in os.walk(str(vault)):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            titles += [os.path.splitext(f)[0] for f in files if f.endswith(".md")]
            if len(titles) >= MAX_NOTES:
                break
    except OSError:
        pass
    return titles[:MAX_NOTES]


def is_new_file(path, tool_response, now=None):
    """True when the write created the file. The tool response decides when it says create or update; else a very young creation time does."""
    if isinstance(tool_response, dict):
        kind = str(tool_response.get("type", "")).lower()
        if kind == "create":
            return True
        if kind == "update":
            return False
    try:
        st = os.stat(path)
        born = getattr(st, "st_birthtime", None) or st.st_ctime
        return (time.time() if now is None else now) - born < NEW_FILE_SECONDS
    except OSError:
        return False


def _inside(path, folder):
    p, d = os.path.normcase(os.path.realpath(str(path))), os.path.normcase(os.path.realpath(str(folder)))
    return p.startswith(d + os.sep)


def log_hit(logs, name, msg):
    """Append one row to the log; rotate it first when it is over 512 KB. Never raises."""
    try:
        logs = Path(logs)
        logs.mkdir(parents=True, exist_ok=True)
        f = logs / LOG_NAME
        try:
            if f.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(str(f), str(f) + ".1")
        except OSError:
            pass
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "file": name, "msg": msg[:200]}) + "\n")
    except Exception:
        pass


def add_context_json(text):
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}})


def build_message(path, scripts_dirs_, vault):
    """The one-line message for a new file at `path`, or "" when nothing similar exists or the file is not a script / note."""
    p = os.path.abspath(path)
    name = os.path.basename(p)
    if p.endswith(".py") and not name.startswith("test_") and any(os.path.dirname(os.path.normcase(p)) == os.path.normcase(os.path.abspath(str(d))) for d in scripts_dirs_):
        found = []
        for d in scripts_dirs_:
            found += similar_scripts(p, str(d))
        found = sorted(set(found), reverse=True)[:2]
        if found:
            return ("[Similar script already exists: %s. Reuse or merge instead of duplicating; search with python scripts/script_index.py.]"
                    % ", ".join("%s (%.2f)" % (f, j) for j, f in found))
        return ""
    if p.endswith(".md") and _inside(p, vault):
        title = os.path.splitext(name)[0]
        n = similar_notes(title, note_titles(vault))
        hits = []
        for d in scripts_dirs_:
            hits += script_hits(title, str(d))
        hits = sorted(set(hits), reverse=True)[:2]
        parts = []
        if n:
            parts.append("notes: " + ", ".join("'%s'" % t for _, t in n))
        if hits:
            parts.append("scripts that already do this kind of thing: " + ", ".join(f for _, f in hits))
        if parts:
            return "[Similar already exists (%s). Merge or link instead of duplicating.]" % "; ".join(parts)
    return ""


def read_payload():
    """The hook JSON from stdin read as UTF-8 bytes. {} on any problem."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}                                  # run by hand in a terminal: nothing to read, do not wait
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def handle(payload, logs=None, dirs=None, vault=None, now=None):
    """-> the JSON text to print, or "" for nothing. Never raises."""
    try:
        logs = Path(logs) if logs else logs_dir()
        if (logs / OFF_NAME).exists() or not isinstance(payload, dict):
            return ""
        if payload.get("agent_id") or payload.get("agent_type"):
            return ""
        tool = payload.get("tool_name")
        if tool and tool != "Write":
            return ""
        tin = payload.get("tool_input") or {}
        path = tin.get("file_path", "") if isinstance(tin, dict) else ""
        if not path or not isinstance(path, str) or not os.path.isfile(path):
            return ""
        if not is_new_file(path, payload.get("tool_response"), now):
            return ""
        msg = build_message(path, dirs if dirs is not None else scripts_dirs(), vault if vault is not None else vault_dir())
        if not msg:
            return ""
        log_hit(logs, os.path.basename(path), msg)
        return add_context_json(msg)
    except Exception:
        return ""


def main():
    try:
        out = handle(read_payload())
        if out:
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
            sys.stdout.write(out + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
