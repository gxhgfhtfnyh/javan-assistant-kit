# -*- coding: utf-8 -*-
"""PostToolUse hook: checks a script the moment the assistant writes it, so a bug is fixed in the same turn.

When the file that was just written or edited is a .py directly inside this assistant's scripts folder, the hook runs script_doctor.check_source() on it
(syntax errors, undefined names, mangled escapes, bad imports, control characters). If it finds something, the hook hands the assistant ONE line as
additional context naming the file and the first findings, and the command to re-check. It stays silent when the file is clean, when the file is
somewhere else, when the tool was not a file write, and on any error. It never blocks a tool and never raises; the exit code is always 0.

Notes that are not findings are left out: the doctor reports "pyflakes not installed, skipped" as a tooling note on every run when the optional package
is missing, and repeating that after every edit would only be noise. Install pyflakes (pip install pyflakes) for the full check.

Hook config: PostToolUse, matcher "Write|Edit|MultiEdit". Reads the hook JSON from stdin as UTF-8 bytes; prints
{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "..."}} only when there is something to say.
Kill switch: logs/doctor_edit_off.txt (the hook does nothing).
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs). Scripts folders checked: ROOT/scripts and this file's own folder.
Pure core (unit tested): relevant(path, scripts_dirs), summarise(name, findings), handle(payload, ...)."""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OFF_NAME = "doctor_edit_off.txt"
WATCHED_TOOLS = ("Write", "Edit", "MultiEdit")
MAX_SHOWN = 4
MAX_READ_BYTES = 2 * 1024 * 1024        # a longer file is not checked: the hook runs after every edit and must stay quick


def root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent)


def logs_dir():
    return Path(os.environ.get("HARNESS_LOGS") or root() / "logs")


def scripts_dirs():
    """The folders whose .py files are checked: ROOT/scripts and the folder this hook lives in (the same folder in a normal install)."""
    out = []
    for d in (root() / "scripts", HERE):
        if d not in out:
            out.append(d)
    return out


def _norm(path):
    return os.path.normcase(os.path.abspath(str(path)))


def relevant(path, dirs=None):
    """True for a .py file directly inside one of the scripts folders (a sub-folder does not count). Case-insensitive only where the file system is."""
    if not path or not str(path).lower().endswith(".py"):
        return False
    folder = os.path.dirname(_norm(path))
    return any(folder == _norm(d) for d in (dirs if dirs is not None else scripts_dirs()))


def real_findings(findings):
    """Drop the tooling notes (an optional checker that is not installed is not a problem in the script)."""
    return [f for f in (findings or []) if f and f[0] != "tooling"]


def summarise(name, findings):
    """One short line naming the file and the first findings; empty string when clean."""
    findings = real_findings(findings)
    if not findings:
        return ""
    shown = "; ".join("%s line %s: %s" % (k, ln, m) for k, ln, m in findings[:MAX_SHOWN])
    more = " (+%d more)" % (len(findings) - MAX_SHOWN) if len(findings) > MAX_SHOWN else ""
    return "[SCRIPT DOCTOR] %s has %d finding(s): %s%s. Fix now, then re-run: python scripts/script_doctor.py check %s" % (name, len(findings), shown, more, name)


def add_context_json(text):
    """The JSON a PostToolUse hook prints so that its text reaches the assistant as additional context."""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}})


def load_doctor():
    """The kit's script_doctor module from this folder, or None when it cannot be imported."""
    try:
        p = str(HERE)
        if p not in sys.path:
            sys.path.insert(0, p)
        import script_doctor
        return script_doctor
    except Exception:
        return None


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


def handle(payload, logs=None, dirs=None, doctor=None):
    """-> the JSON text to print, or "" for nothing. Never raises."""
    try:
        logs = Path(logs) if logs else logs_dir()
        if (logs / OFF_NAME).exists():
            return ""
        if not isinstance(payload, dict):
            return ""
        tool = payload.get("tool_name")
        if tool and tool not in WATCHED_TOOLS:
            return ""
        tin = payload.get("tool_input") or {}
        path = tin.get("file_path", "") if isinstance(tin, dict) else ""
        if not path or not relevant(path, dirs) or not os.path.isfile(path):
            return ""
        doctor = doctor or load_doctor()
        if doctor is None:
            return ""
        with open(path, "rb") as f:
            text = f.read(MAX_READ_BYTES + 1)
        if len(text) > MAX_READ_BYTES:
            return ""
        name = os.path.basename(path)
        msg = summarise(name, doctor.check_source(text.decode("utf-8-sig", errors="replace"), name))
        return add_context_json(msg) if msg else ""
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
