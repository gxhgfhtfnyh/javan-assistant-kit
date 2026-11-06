# -*- coding: utf-8 -*-
"""Known binaries check: a checksum registry for the programs the assistant depends on.

A silent swap (a trusted program replaced by a different file at the same path) is invisible to a "does the file still exist" check. This keeps the SHA-256 of each
registered program in logs/known_binaries.json and compares on every run. The first run registers what it finds of: the Python that runs it, the `claude` command
and `git`. Anything else you rely on you add yourself, once, right after you have checked where it came from:

    python scripts/known_binaries_check.py                  # verify every registered program (the timed job)
    python scripts/known_binaries_check.py --add PATH       # register (or re-register) a program with its CURRENT hash
    python scripts/known_binaries_check.py --remove PATH
    python scripts/known_binaries_check.py --list

A program update legitimately changes its hash: after you have updated Python, git or claude on purpose, run --add PATH for it once, otherwise the next run reports it as
changed. A changed or missing program raises one alert (and a work item for you). If the registry file itself is unreadable the script alerts and does NOT recreate it,
because re-seeding would trust whatever is there now. Log: logs/known_binaries_check.jsonl. Kill switch: logs/known_binaries_check_off.txt.
Pure core (unit tested): sha256_file, load_registry, verify, seed_paths, check. The script only reads the programs; nothing is executed."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

NAME = "known_binaries_check"
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

REGISTRY_NAME = "known_binaries.json"
LOG_NAME = "known_binaries_check.jsonl"
OFF_NAME = "known_binaries_check_off.txt"
MAX_REGISTRY_BYTES = 2_000_000
MAX_ENTRIES = 500
MAX_HASH_BYTES = 4 * 1024 ** 3
MAX_LOG_BYTES = 1_000_000
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def logs_dir():
    return Path(LOGS)


def _alert(text, **kw):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        return notify.alert(text, source=NAME, logs=str(logs_dir()), **kw)
    except Exception:
        return None


def sha256_file(path):
    """-> hex digest of the file, or None when it does not exist / is not a regular file. Raises OSError when it cannot be read and ValueError when it is over the size cap."""
    p = Path(path)
    if not p.is_file():
        return None
    if p.stat().st_size > MAX_HASH_BYTES:
        raise ValueError("file is larger than the %d byte cap" % MAX_HASH_BYTES)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def seed_paths():
    """The programs registered on the first run: this Python, the claude command and git (whichever exist)."""
    out = []
    for cand in (sys.executable, shutil.which("claude"), shutil.which("git")):
        if cand and os.path.isfile(cand):
            ab = os.path.abspath(cand)
            if ab not in out:
                out.append(ab)
    return out


def load_registry(path):
    """-> (registry dict path->hash, status 'missing'|'ok'|'corrupt'). Entries that are not '<text>: <64 hex>' are dropped; more than MAX_ENTRIES counts as corrupt."""
    p = Path(path)
    try:
        if p.stat().st_size > MAX_REGISTRY_BYTES:
            return {}, "corrupt"
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, ValueError):
        return {}, "corrupt"
    if not isinstance(raw, dict) or len(raw) > MAX_ENTRIES:
        return {}, "corrupt"
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and HEX64.match(v)}, "ok"


def save_registry(path, registry):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def verify(registry):
    """Pure-ish. -> (mismatched, missing, unreadable) path lists."""
    mismatched, missing, unreadable = [], [], []
    for path, expected in sorted(registry.items()):
        try:
            actual = sha256_file(path)
        except (OSError, ValueError):
            unreadable.append(path)
            continue
        if actual is None:
            missing.append(path)
        elif actual != expected:
            mismatched.append(path)
    return mismatched, missing, unreadable


def append_log(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".old"))
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def check(dry_run=False, now=None):
    now = time.time() if now is None else now
    logs = logs_dir()
    if (logs / OFF_NAME).exists():
        return {"off": True}
    reg_path = logs / REGISTRY_NAME
    registry, status = load_registry(reg_path)
    result = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), "status": status, "seeded": [], "checked": 0, "mismatched": [], "missing": [],
              "unreadable": [], "dry_run": bool(dry_run)}
    if status == "corrupt":
        if not dry_run:
            _alert("the known-binaries registry cannot be read, so nothing was verified (it was left untouched; move it aside to start a new one)",
                   intent_kind="binary_registry_unreadable", tier=2, value=4, dedupe="knownbin-registry-corrupt")
        return result
    if status == "missing":
        for p in seed_paths():
            try:
                h = sha256_file(p)
            except (OSError, ValueError):
                h = None
            if h:
                registry[p] = h
        result["seeded"] = sorted(registry)
        if not dry_run:
            save_registry(reg_path, registry)
    mismatched, missing, unreadable = verify(registry) if status == "ok" else ([], [], [])
    result.update(checked=len(registry), mismatched=mismatched, missing=missing, unreadable=unreadable)
    if dry_run:
        return result
    append_log(logs / LOG_NAME, result)
    if mismatched or missing:
        names = [Path(x).name for x in (mismatched + missing)]
        _alert("trusted program changed or gone: %s (if you updated it on purpose, run known_binaries_check.py --add <path> after checking where it came from)"
               % ", ".join(names[:3]), intent_kind="binary_changed", tier=2, value=5, dedupe="knownbin-" + hashlib.sha1("|".join(sorted(mismatched + missing)).encode("utf-8")).hexdigest()[:10])
    return result


def add_binary(path, logs=None):
    """Register (or re-register) one program with its current hash. -> (ok, message)."""
    logs = Path(logs or logs_dir())
    reg_path = logs / REGISTRY_NAME
    registry, status = load_registry(reg_path)
    if status == "corrupt":
        return False, "the registry cannot be read; move %s aside first" % reg_path.name
    full = os.path.abspath(str(path))
    try:
        h = sha256_file(full)
    except (OSError, ValueError) as e:
        return False, "cannot read %s: %s" % (full, type(e).__name__)
    if h is None:
        return False, "%s is not an existing file, nothing to register" % full
    if len(registry) >= MAX_ENTRIES and full not in registry:
        return False, "the registry is full (%d entries)" % MAX_ENTRIES
    registry[full] = h
    save_registry(reg_path, registry)
    return True, "registered %s\n  sha256: %s" % (full, h)


def remove_binary(path, logs=None):
    logs = Path(logs or logs_dir())
    reg_path = logs / REGISTRY_NAME
    registry, status = load_registry(reg_path)
    full = os.path.abspath(str(path))
    if status != "ok" or full not in registry:
        return False, "%s is not registered" % full
    del registry[full]
    save_registry(reg_path, registry)
    return True, "removed %s" % full


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog=NAME, description="verify the SHA-256 of trusted programs")
    ap.add_argument("--add", metavar="PATH")
    ap.add_argument("--remove", metavar="PATH")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="verify and print, but write no registry, log or alert")
    a = ap.parse_args(argv)
    try:
        if a.add:
            ok, msg = add_binary(a.add)
            print(("[REGISTERED] " if ok else "[ERROR] ") + msg)
            return 0 if ok else 1
        if a.remove:
            ok, msg = remove_binary(a.remove)
            print(("[REMOVED] " if ok else "[ERROR] ") + msg)
            return 0 if ok else 1
        if a.list:
            reg, status = load_registry(logs_dir() / REGISTRY_NAME)
            for p, h in sorted(reg.items()):
                print("%s  %s" % (h[:16], p))
            print("%d registered (%s)" % (len(reg), status))
            return 0
        r = check(dry_run=a.dry_run)
    except Exception as e:
        print("[ERROR] %s: %s" % (type(e).__name__, str(e)[:120]))
        return 0
    if r.get("off"):
        print("[OFF] logs/%s exists" % OFF_NAME)
    elif r["status"] == "corrupt":
        print("[ERROR] the registry file cannot be read; nothing verified")
    elif r["mismatched"] or r["missing"]:
        print("[CHANGED] mismatched=%s missing=%s" % (r["mismatched"], r["missing"]))
    elif r["seeded"]:
        print("[SEEDED] %d program(s) registered: %s" % (len(r["seeded"]), r["seeded"]))
    else:
        print("[OK] %d registered program(s) all match their trusted hash%s" % (r["checked"], " (%d unreadable)" % len(r["unreadable"]) if r["unreadable"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
