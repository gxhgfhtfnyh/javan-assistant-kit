# -*- coding: utf-8 -*-
"""Integrity check: has anything that controls the assistant been changed behind its back?

Watches a small set of files by hash: CLAUDE.md, .claude/settings.json, every hook script that
settings.json runs (read from its "command" fields), and the scripts that gate, schedule and
remember (send_gate, stop_gate, session_start, intent_store, approve, job_runner, harness_health,
quarantine, memory_write, this file) plus scripts/jobs.json.

The first run writes logs/integrity_baseline.json. Every later run compares against it. A file that
is modified, missing, or newly watched is a change: one tier-2 value-4 item goes into the work list
(ask the owner first), one line goes to logs/alerts.txt, and the same change (same file, same
content) is alarmed once until the owner accepts the new state with --rebaseline.

    python integrity_check.py              # compare against the baseline (exit 1 when something changed)
    python integrity_check.py --job        # same, but always exit 0: the alarm is the signal, and a
                                           # nonzero exit would make the scheduler think the job broke
    python integrity_check.py --rebaseline # accept the current files as the new baseline
    python integrity_check.py --list       # show every watched file and its status

Paths inside the harness folder are stored relative to it, so moving the folder is not a change.
"""
import datetime
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# watched scripts, relative to the scripts folder
CORE_SCRIPTS = [
    "send_gate.py", "stop_gate.py", "session_start.py", "intent_store.py", "approve.py",
    "job_runner.py", "harness_health.py", "quarantine.py", "memory_write.py", "integrity_check.py", "jobs.json",
]
SCRIPT_EXTS = (".py", ".ps1", ".bat", ".cmd", ".vbs", ".sh")
TOKEN_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'|(\S+)')


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or (_root() / "logs")).resolve()


def hook_commands(settings):
    """Pure. Every "command" string anywhere in a parsed settings.json, flat or nested hook shape."""
    out = []

    def walk(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get("command"), str):
                out.append(obj["command"])
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(settings)
    return out


def script_paths(command):
    """Pure. The script files named in one hook command line. A quoted path may contain spaces
    ("D:/My Assistant/scripts/send_gate.py"); an unquoted one ends at the next space."""
    found = []
    for double, single, bare in TOKEN_RE.findall(command or ""):
        token = (double or single or bare).strip()
        if token.lower().endswith(SCRIPT_EXTS) and token not in found:
            found.append(token)
    return found


def _hook_paths_from_settings():
    p = _root() / ".claude" / "settings.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    paths = set()
    for cmd in hook_commands(data):
        paths.update(script_paths(cmd))
    return sorted(paths)


def _resolve_script_path(path_str):
    """A hook script path as an absolute path: as written, else under scripts/, else under the root."""
    p = Path(path_str)
    if p.is_absolute():
        return p.resolve()
    for base in (_root() / "scripts", _root()):
        cand = (base / p).resolve()
        if cand.exists():
            return cand
    return (_root() / p).resolve()


def _critical_files():
    root = _root()
    files = [root / "CLAUDE.md", root / ".claude" / "settings.json"]
    files += [_resolve_script_path(h) for h in _hook_paths_from_settings()]
    files += [root / "scripts" / s for s in CORE_SCRIPTS]
    return sorted({_key(f) for f in files})


def _key(path):
    """A file's name in the baseline: relative to the harness folder when it is inside it, else absolute."""
    p = Path(path)
    try:
        return p.resolve().relative_to(_root()).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def _abs(key):
    p = Path(key)
    return p if p.is_absolute() else _root() / p


def _sha256(path):
    """Hex digest of a file, or None when it does not exist or cannot be read."""
    p = Path(path)
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _alert_line(key, sha):
    return "%s@%s" % (key, (sha or "missing")[:12])


def _raise_alarm(changes, current):
    """One work-list item and one alerts.txt line per change not yet alarmed. A change is the file AND its content,
    so a second, different edit of an already-alarmed file alarms again."""
    logs = _logs()
    alarmed_path = logs / "integrity_alarmed.json"
    alarmed = set((_read_json(alarmed_path) or {}).get("alarmed", []))
    fresh = [c for c in changes if _alert_line(c, current.get(c)) not in alarmed]
    if not fresh:
        return
    note = ""
    try:
        sys.path.insert(0, str(HERE))
        import intent_store
        rows = intent_store.read_jsonl("intents.jsonl", logs=str(logs))
        cand = intent_store.new_intent(
            "integrity_alert", "Integrity check: %s changed since the baseline. Ask the owner whether this was intended; if yes run integrity_check.py --rebaseline." % ", ".join(fresh),
            tier=2, value=4, dedupe="integrity-" + "-".join(_alert_line(c, current.get(c)) for c in sorted(fresh)))
        if not intent_store.is_duplicate(rows, cand):
            rows.append(cand)
            intent_store.write_intents(rows, logs=str(logs))
    except Exception as e:                                   # the alert line below still goes out
        note = " (work-list item not written: %s)" % type(e).__name__
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "alerts.txt", "a", encoding="utf-8") as f:
        for c in fresh:
            f.write("%s INTEGRITY CHANGE: %s%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), c, note))
    alarmed.update(_alert_line(c, current.get(c)) for c in fresh)
    _write_json(alarmed_path, {"alarmed": sorted(alarmed)})


def check(rebaseline=False, list_only=False):
    """-> {"status": "OK" | "BASELINE_SET" | "TAMPERED" | "LIST", ...}. Writes only the baseline, the alarm record,
    alerts.txt and one work-list item; it never touches a watched file."""
    keys = _critical_files()
    current = {k: _sha256(_abs(k)) for k in keys}
    logs = _logs()
    baseline_path = logs / "integrity_baseline.json"
    baseline = _read_json(baseline_path)
    old = (baseline or {}).get("hashes", {}) if baseline else {}

    if list_only:
        rows = []
        for k in keys:
            h = current[k]
            status = "MISSING" if h is None else "NEW" if old.get(k) is None else "MODIFIED" if h != old[k] else "OK"
            rows.append({"path": k, "status": status, "hash": h})
        return {"status": "LIST", "watched": len(keys), "files": rows}

    if baseline is None and baseline_path.exists() and not rebaseline:
        # a baseline that is there but cannot be read is a change, not a first run: re-baselining silently would accept whatever was edited
        name = "logs/" + baseline_path.name
        _raise_alarm([name], {name: _sha256(baseline_path) or "unreadable"})
        return {"status": "TAMPERED", "modified": [name], "missing": [], "new": [], "watched": len(keys)}

    if rebaseline or baseline is None:
        _write_json(baseline_path, {"built": datetime.datetime.now().isoformat(timespec="seconds"), "hashes": current})
        _write_json(logs / "integrity_alarmed.json", {"alarmed": []})
        return {"status": "BASELINE_SET", "watched": len(keys), "files": keys}

    modified, missing, new = [], [], []
    for k, h in current.items():
        was = old.get(k)
        if h is None and was is None:
            continue                                        # never existed in either scan
        if h is None:
            missing.append(k)
        elif was is None:
            new.append(k)
        elif h != was:
            modified.append(k)
    for k, was in old.items():
        if k not in current and was is not None:
            missing.append(k)                               # a baselined file is no longer watched
    if modified or missing or new:
        _raise_alarm(modified + missing + new, current)
        return {"status": "TAMPERED", "modified": modified, "missing": missing, "new": new, "watched": len(keys)}
    return {"status": "OK", "watched": len(keys), "new": []}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
        return 0
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    r = check(rebaseline="--rebaseline" in argv, list_only="--list" in argv)
    if r["status"] == "LIST":
        print("Watching %d files:" % r["watched"])
        for f in r["files"]:
            print("  %-9s %s" % (f["status"], f["path"]))
        return 0
    if r["status"] == "TAMPERED":
        print("[TAMPERED] modified=%s missing=%s new=%s" % (r["modified"], r["missing"], r["new"]))
        return 0 if "--job" in argv else 1
    if r["status"] == "BASELINE_SET":
        print("[BASELINE_SET] watching %d files" % r["watched"])
        return 0
    print("[OK] %d files unchanged" % r["watched"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
