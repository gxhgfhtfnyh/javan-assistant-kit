# -*- coding: utf-8 -*-
"""PreToolUse + PostToolUse hook: one JSON line per tool call in logs/activity_log.jsonl, so there is a record of what the assistant actually did.

Two kinds of rows share the file and are joined by "id" (the tool_use_id):
  {"ev": "call",   "ts", "tool", "detail", "id", "agent", "agent_type", "sid", "path"?, "plus"?, "minus"?, "diff"?, "is_test"?}   written before the tool runs
  {"ev": "result", "ts", "id", "ok", "ms"}                                                                                 written after it finished
A call row with no result row is a call that failed, was denied, or is still running. "detail" is the command, the path, the pattern, the URL or the
query, cut to 110 characters. "plus"/"minus" are the lines an Edit or Write added and removed (counts only). "is_test" is true when a shell command is
a test run (pytest, unittest, npm test, cargo test, go test, py_compile ...). "agent" is the sub-agent's id when a sub-agent made the call, else "".

Privacy: secret-looking text (keys, bearer tokens, passwords in a command or a URL) is replaced by [redacted] before anything is written, and the text of an
edit is NOT stored. To also store a short unified diff of every Edit (at most 60 lines, redacted), create logs/activity_log_diffs_on.txt.
The log stays in logs/ on this computer; nothing is sent anywhere.

Safe to run in parallel: each row is appended with ONE write on an append-mode handle (on Windows a FILE_APPEND_DATA handle, so two processes cannot
overwrite each other), and a row over 48 KB drops its optional fields or is skipped. The file is moved to activity_log.jsonl.1 once it is over 10 MB.
The hook prints nothing, never blocks a tool, never raises, and always exits 0. Kill switch: logs/activity_log_off.txt.

Hook config: PreToolUse and PostToolUse, matcher "*" on both (the same script; it reads hook_event_name from the input). An optional first argument
pre|post|fail overrides that, e.g. for a PostToolUseFailure hook (then the result row has ok false).
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): redact, detail_of, is_test_command, edit_diff, build_call_entry, build_result_entry, append_line, handle."""
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OFF_NAME = "activity_log_off.txt"
LOG_NAME = "activity_log.jsonl"
DIFFS_ON_NAME = "activity_log_diffs_on.txt"
MAX_DETAIL = 110
MAX_DIFF_LINES = 60                 # a unified diff, capped so one huge Edit cannot flood the log
MAX_DIFF_CHARS = 20000              # no diff at all above this many characters (old + new): this hook runs on every tool call and must stay fast
MAX_DIFF_SIDE_LINES = 400
MAX_LINE_BYTES = 48 * 1024
ROTATE_BYTES = 10 * 1024 * 1024
MAX_COMMAND_SCAN = 2000             # characters of a shell command looked at to decide whether it is a test run

_REDACTED = "[redacted]"
_SECRET_RULES = [
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic|token)\s+)?[^\s\"']+"), r"\1" + _REDACTED),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + _REDACTED),
    (re.compile(r"(?i)([?&](?:token|key|api_key|apikey|secret|sig|signature|auth|password|access_token)=)[^&\s\"']+"), r"\1" + _REDACTED),
    (re.compile(r"(?i)(://[^/\s:@]+:)[^/\s@]+@"), r"\1" + _REDACTED + "@"),
    (re.compile(r"(?i)(--?(?:password|passwd|token|secret|api-?key|auth)[= ])\S+"), r"\1" + _REDACTED),
    (re.compile(r"(?i)\b(authorization|api[_-]?key|apikey|token|secret|passwd|password|pwd|access[_-]?key|client[_-]?secret)(\"?'?\s*[:=]\s*\"?'?)[^\s\"'&;,]{4,}"), r"\1\2" + _REDACTED),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}"
                r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"), _REDACTED),
]


def root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent)


def logs_dir():
    return Path(os.environ.get("HARNESS_LOGS") or root() / "logs")


def redact(text):
    """The text with secret-looking parts replaced by [redacted]. Never raises."""
    try:
        out = str(text)
        for rx, repl in _SECRET_RULES:
            out = rx.sub(repl, out)
        return out
    except Exception:
        return ""


# ---------------------------------------------------------------- is this shell command a test run?
_TOKEN = re.compile(r"""(?:[^\s"']|"[^"]*"|'[^']*')+""")
_OP_SPLIT = re.compile(r"(&&|\|\||;|\||&)")
_PY_TESTS = {"pytest", "unittest", "py_compile", "nose", "nose2", "tox"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _split_segments(command):
    """Token lists, one per simple command, split on && || ; | & outside quotes."""
    segs, cur = [], []
    for tok in _TOKEN.findall(command):
        parts = [tok] if tok[0] in "\"'" else _OP_SPLIT.split(tok)
        for part in parts:
            if part == "":
                continue
            if _OP_SPLIT.fullmatch(part):
                if cur:
                    segs.append(cur)
                cur = []
            else:
                cur.append(part)
    if cur:
        segs.append(cur)
    return segs


def _base(tok):
    t = tok.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat"):
        if t.endswith(ext):
            t = t[: -len(ext)]
    return t


def _first_words(args, n=2):
    return [a.strip("\"'") for a in args if not a.startswith("-")][:n]


def is_test_command(command):
    """True when the first real program of a shell command (after cd X &&, NAME=value prefixes) is a test runner.
    Never because a test word is merely an argument of another program (grep, git, echo...)."""
    try:
        for seg in _split_segments(str(command or "")[:MAX_COMMAND_SCAN]):
            i = 0
            while i < len(seg) and _ENV_ASSIGN.match(seg[i]):
                i += 1
            if i >= len(seg):
                continue                                        # only env assignments
            prog = _base(seg[i])
            if prog in ("cd", "pushd", "popd", "export", "set"):
                continue
            args = seg[i + 1:]
            if prog in ("pytest", "py.test", "py_compile", "tox"):
                return True
            if prog in ("node", "nodejs"):
                return "--test" in [a.strip("\"'") for a in args]
            if prog in ("npm", "yarn", "pnpm"):
                w = _first_words(args)
                return bool(w) and (w[0] in ("test", "t") or (w[0] == "run" and len(w) > 1 and w[1].startswith("test")))
            if prog in ("cargo", "go"):
                return _first_words(args, 1) == ["test"]
            if prog in ("python", "py") or re.fullmatch(r"python\d(\.\d+)?", prog):
                j = 0
                while j < len(args):
                    a = args[j].strip("\"'")
                    if a == "-m":
                        return j + 1 < len(args) and _base(args[j + 1]) in _PY_TESTS
                    if a == "-c":
                        return False
                    if a.startswith("-"):
                        j += 1
                        continue
                    name = _base(a)
                    if name.endswith(".py"):
                        name = name[:-3]
                    return name in _PY_TESTS or name.startswith("test_") or name.endswith("_test")
                return False
            return False
        return False
    except Exception:
        return False


# ---------------------------------------------------------------- what a call looked like
def detail_of(tool, ti):
    """The short description of what the call did, from its input."""
    if not isinstance(ti, dict):
        return ""
    if tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        return ti.get("file_path") or ti.get("notebook_path") or ""
    if tool in ("Bash", "PowerShell"):
        return ti.get("command", "")
    if tool == "Grep":
        return '"%s" in %s' % (ti.get("pattern", ""), ti.get("path", "."))
    if tool == "Glob":
        return ti.get("pattern", "")
    if tool == "WebFetch":
        return ti.get("url", "")
    if tool == "WebSearch":
        return ti.get("query", "")
    if tool in ("Agent", "Task"):
        return ti.get("description", "")
    for k in ("skill", "query", "prompt", "text", "command", "url", "path"):
        if ti.get(k):
            return str(ti[k])
    return ""


def clean_detail(raw):
    """The detail as one redacted line of at most MAX_DETAIL characters."""
    detail = redact(str(raw)).replace("\r", " ").replace("\n", " ")
    return detail[: MAX_DETAIL - 3] + "..." if len(detail) > MAX_DETAIL else detail


def line_counts(tool, ti):
    """(plus, minus) line counts for an Edit or a Write; None for other tools. Counts only, never the text."""
    try:
        if tool == "Edit":
            return len((ti.get("new_string") or "").splitlines()), len((ti.get("old_string") or "").splitlines())
        if tool == "Write":
            return len((ti.get("content") or "").splitlines()), 0
    except Exception:
        pass
    return None


def edit_diff(ti):
    """A redacted unified diff of an Edit's old and new text, capped; None when there is nothing to show or it is too big."""
    import difflib
    import itertools
    old, new = ti.get("old_string") or "", ti.get("new_string") or ""
    if not isinstance(old, str) or not isinstance(new, str) or (not old and not new) or len(old) + len(new) > MAX_DIFF_CHARS:
        return None
    a, b = old.splitlines(), new.splitlines()
    if len(a) > MAX_DIFF_SIDE_LINES or len(b) > MAX_DIFF_SIDE_LINES:
        return None
    lines = list(itertools.islice(difflib.unified_diff(a, b, lineterm="", n=1), MAX_DIFF_LINES + 1))
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + ["... (truncated)"]
    return redact("\n".join(lines))


def build_call_entry(payload, diffs=False, now=None):
    """The row for a call about to run. None when the payload names no tool."""
    tool = payload.get("tool_name", "")
    if not tool or not isinstance(tool, str):
        return None
    ti = payload.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    raw = str(detail_of(tool, ti))
    entry = {"ev": "call", "ts": round(time.time() if now is None else now, 3), "tool": tool, "detail": clean_detail(raw),
             "id": str(payload.get("tool_use_id", "") or ""), "agent": str(payload.get("agent_id", "") or ""),
             "agent_type": str(payload.get("agent_type", "") or ""), "sid": str(payload.get("session_id", "") or "")[:8]}
    p = ti.get("file_path") or ti.get("notebook_path") or ""
    if p:
        entry["path"] = str(p).replace("\\", "/")
    counts = line_counts(tool, ti)
    if counts:
        entry["plus"], entry["minus"] = counts
    if diffs and tool == "Edit":
        d = edit_diff(ti)
        if d:
            entry["diff"] = d
    if tool in ("Bash", "PowerShell") and is_test_command(raw):
        entry["is_test"] = True
    return entry


def build_result_entry(payload, ok=True, now=None):
    """The row for a call that finished."""
    ms = payload.get("duration_ms")
    return {"ev": "result", "ts": round(time.time() if now is None else now, 3), "id": str(payload.get("tool_use_id", "") or ""), "ok": bool(ok),
            "ms": ms if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None}


def response_ok(payload):
    """False when a tool response says it was an error; True otherwise (a PostToolUse event means the tool ran)."""
    resp = payload.get("tool_response")
    if isinstance(resp, dict):
        return not (resp.get("is_error") or resp.get("error") or resp.get("interrupted"))
    return True


def mode_of(payload, argv=()):
    """'pre', 'post' or 'fail': the first argument when it is one of those, else from hook_event_name."""
    if argv and str(argv[0]).lower() in ("pre", "post", "fail"):
        return str(argv[0]).lower()
    event = str(payload.get("hook_event_name", "")).lower()
    if event == "posttooluse":
        return "post"
    if event == "posttoolusefailure":
        return "fail"
    if event == "pretooluse":
        return "pre"
    return "post" if "tool_response" in payload else "pre"


# ---------------------------------------------------------------- append-safe writing
def use_win_append():
    return os.name == "nt"


def _win_append(path, blob):
    """Append on Windows through a handle opened with FILE_APPEND_DATA only, so the OS places every write at the current end of file.
    The C runtime's append mode only seeks to the end before writing, which lets two processes overwrite each other.
    True when the row was written (or partly written: a retry would tear it); False when no handle could be opened or nothing was written."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.CreateFileW(path, 0x0004, 0x1 | 0x2 | 0x4, None, 4, 0x80, None)      # FILE_APPEND_DATA, share all, OPEN_ALWAYS, NORMAL
        if handle is None or handle == wintypes.HANDLE(-1).value:
            return False
        try:
            written = wintypes.DWORD(0)
            k32.WriteFile(handle, blob, len(blob), ctypes.byref(written), None)
            return written.value > 0
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


def _encode(entry):
    """The row as UTF-8 bytes with a newline; drops the optional fields when it is over MAX_LINE_BYTES; None when it still does not fit."""
    blob = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    if len(blob) <= MAX_LINE_BYTES:
        return blob
    entry = dict(entry)
    for k in ("diff", "path", "detail"):
        entry.pop(k, None)
        blob = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        if len(blob) <= MAX_LINE_BYTES:
            return blob
    return None


def append_line(path, entry, rotate_bytes=ROTATE_BYTES):
    """Append one JSON row with a single write; rotate to <name>.1 first when the file is oversized. True when written. Never raises."""
    try:
        path = Path(path)
        blob = _encode(entry)
        if blob is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > rotate_bytes:
                os.replace(str(path), str(path) + ".1")
        except OSError:
            pass
        if use_win_append() and _win_append(str(path), blob):
            return True
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


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


def handle(payload, mode=None, logs=None, now=None):
    """Write the row for this event. -> the row that was written, or None. Never raises."""
    try:
        logs = Path(logs) if logs else logs_dir()
        if not isinstance(payload, dict) or (logs / OFF_NAME).exists():
            return None
        mode = mode or mode_of(payload)
        if mode in ("post", "fail"):
            if not payload.get("tool_name"):
                return None
            entry = build_result_entry(payload, ok=(mode == "post" and response_ok(payload)), now=now)
        else:
            entry = build_call_entry(payload, diffs=(logs / DIFFS_ON_NAME).exists(), now=now)
        if entry is None:
            return None
        return entry if append_line(logs / LOG_NAME, entry) else None
    except Exception:
        return None


def main(argv=None):
    try:
        argv = sys.argv[1:] if argv is None else argv
        payload = read_payload()
        handle(payload, mode=mode_of(payload, argv))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
