# -*- coding: utf-8 -*-
"""Capability sensor: finds out that the assistant can no longer do something BEFORE the user asks for it.

A missing capability is usually discovered only when someone asks for the thing and gets "I can't". This keeps a registry of what the assistant relies on,
checks each entry with a cheap read-only probe on every run, and turns a red one into a work item early: a tier-1 item with a fix hint when the assistant can
repair it itself, or exactly one tier-2 question with the precise human action when only the user can (a password, an install). An entry must be red on two
runs in a row to count, so a service that is merely restarting is not a gap. The other half of the idea is in claim_gate.py: any "I can't" in a reply becomes
a capability_gap work item.

    python sense_capabilities.py [--dry-run]    # --dry-run prints every capability green or RED and writes nothing
    python sense_capabilities.py --init         # writes the starter registry to logs/capabilities.json so you can edit it (never overwrites)
Kill switch: logs/sense_capabilities_off.txt (a dry run ignores it). State: logs/sense_capabilities_state.json.

The registry is built in (four entries about the starter kit itself: the Claude Code command, the vault folder, the work list, the send gate). Your own entries
go in logs/capabilities.json; they are added to the built-in ones, an entry with the same id replaces it, and {"id": "claude_cli", "disabled": true} removes one:
    [{"id": "tts", "name": "text to speech", "probe": {"kind": "which", "arg": "espeak"}, "fix": "install espeak"},
     {"id": "mail_key", "name": "the mail account", "probe": {"kind": "cred", "arg": "my-mail-service", "user": "me"}, "needs_human": "store the mail password in the OS keychain"}]
An entry needs id, name, probe and either `fix` (the assistant can do it) or `needs_human` (the one thing only the user can do; optional `ask_dedupe`).
Probe kinds (a closed set; nothing in the registry is ever executed as a command):
    exists    arg is a path; scripts/ logs/ vault/ at the start mean those folders of this assistant
    fresh     arg is a path, `minutes` the longest it may go without changing (default 60)
    http      arg is an http(s) URL; any answer, even 401 or 404, means the service is up
    proc      arg is a script name that must appear in a running process's command line
    which     arg is a program that must be on PATH
    cred      arg is a keyring service name, `user` the account; only whether a secret EXISTS is checked, it is never read out (needs: pip install keyring)
    task      Windows only: arg is a scheduled task that must be Ready or Running      task_ok  Windows only: its last result is clean
A Windows-only probe on another system reports "not applicable" and counts as green, so it never raises a false alarm.
Pure core (unit tested): resolve, judge, intent_for, validate_entry, merge_registry."""
import json
import os
import re
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
ROOT = Path(os.environ.get("HARNESS_ROOT") or SCRIPTS.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

OFF_NAME = "sense_capabilities_off.txt"
STATE_NAME = "sense_capabilities_state.json"
REGISTRY_NAME = "capabilities.json"
BENIGN = {"0", "267011", "267009", "267014", ""}
KINDS = ("exists", "fresh", "http", "proc", "which", "cred", "task", "task_ok")
WINDOWS_ONLY = ("task", "task_ok")
MAX_REGISTRY = 200 * 1024
DEFAULT_REGISTRY = [
    {"id": "claude_cli", "name": "the Claude Code command (the autopilot needs it)", "probe": {"kind": "which", "arg": "claude"},
     "needs_human": "Install Claude Code and make sure the command `claude` runs in a terminal, then tell me."},
    {"id": "vault", "name": "the memory vault folder", "probe": {"kind": "exists", "arg": "vault/"},
     "fix": "recreate the vault folder or restore it from the last backup"},
    {"id": "work_list", "name": "the work list (intent store)", "probe": {"kind": "exists", "arg": "scripts/intent_store.py"},
     "fix": "restore scripts/intent_store.py from the starter kit"},
    {"id": "send_gate", "name": "the send gate (holds anything another person would see)", "probe": {"kind": "exists", "arg": "scripts/send_gate.py"},
     "fix": "restore scripts/send_gate.py from the starter kit and check that it is still wired in .claude/settings.json"},
]


def logs_dir():
    env = os.environ.get("HARNESS_LOGS")
    if env:
        return Path(env).resolve()
    root_env = os.environ.get("HARNESS_ROOT")
    return (Path(root_env) / "logs").resolve() if root_env else LOGS


def scripts_dir():
    return SCRIPTS


def vault_dir():
    env = os.environ.get("HARNESS_VAULT")
    if env:
        return Path(env).resolve()
    root_env = os.environ.get("HARNESS_ROOT")
    return (Path(root_env) / "vault").resolve() if root_env else ROOT / "vault"


# ---------- pure core ----------
def resolve(path, scripts=None, logs=None, vault=None):
    """Pure. A path starting scripts/, logs/ or vault/ is inside that folder of this assistant; anything else is used as given."""
    p = str(path).replace("\\", "/")
    for prefix, base in (("scripts/", scripts), ("logs/", logs), ("vault/", vault)):
        if p.startswith(prefix) and base is not None:
            return os.path.join(str(base), *[x for x in p[len(prefix):].split("/") if x])
    return str(path)


def intent_for(cap, detail, week):
    """Pure. -> (tier, value, what, dedupe) for a capability that stayed red."""
    if cap.get("needs_human"):
        return (2, 3, "ASK (only the user can): capability '%s' is red (%s). %s" % (cap["name"], detail, cap["needs_human"]),
                cap.get("ask_dedupe") or "cap-ask-%s-%s" % (cap["id"], week))
    return (1, 4, "Capability '%s' is red: %s. Fix: %s" % (cap["name"], detail, cap.get("fix", "find out why and restore it")), "cap-red-%s-%s" % (cap["id"], time.strftime("%Y%m%d")))


def judge(registry, results, state, week):
    """Pure. results: {id: (ok, detail)}. -> (new_state, [intent tuples]). Raises only after two red runs in a row; a green run resets the count."""
    new, out = {}, []
    for cap in registry:
        ok, detail = results.get(cap["id"], (False, "no probe result"))
        try:
            before = int((state.get(cap["id"]) or {}).get("reds", 0))
        except (TypeError, ValueError, AttributeError):
            before = 0
        reds = 0 if ok else before + 1
        new[cap["id"]] = {"reds": reds, "ok": bool(ok), "detail": detail}
        if reds == 2 or (reds > 2 and reds % 96 == 0):       # once on the second red run, then a reminder about daily at a 15 minute tick
            out.append(intent_for(cap, detail, week))
    return new, out


def validate_entry(e):
    """Pure. -> (clean entry or None, problem text or ''). Only the known keys are kept, with their size limits."""
    if not isinstance(e, dict):
        return None, "an entry is not an object"
    eid = e.get("id")
    if not isinstance(eid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", eid):
        return None, "an entry has no usable id (letters, digits, _ . - up to 40)"
    if e.get("disabled") is True:
        return {"id": eid, "disabled": True}, ""
    probe = e.get("probe")
    if not isinstance(e.get("name"), str) or not e["name"].strip() or not isinstance(probe, dict):
        return None, "%s needs a name and a probe" % eid
    if probe.get("kind") not in KINDS:
        return None, "%s: unknown probe kind %r (use one of %s)" % (eid, probe.get("kind"), ", ".join(KINDS))
    if not (e.get("fix") or e.get("needs_human")):
        return None, "%s needs a `fix` hint or a `needs_human` ask" % eid
    clean_probe = {"kind": probe["kind"], "arg": str(probe.get("arg", ""))[:300]}
    if "user" in probe:
        clean_probe["user"] = str(probe["user"])[:100]
    if "minutes" in probe:
        try:
            clean_probe["minutes"] = max(1, int(probe["minutes"]))
        except (TypeError, ValueError):
            return None, "%s: minutes must be a number" % eid
    clean = {"id": eid, "name": e["name"].strip()[:100], "probe": clean_probe}
    for k in ("fix", "needs_human"):
        if e.get(k):
            clean[k] = str(e[k])[:300]
    if e.get("ask_dedupe"):
        clean["ask_dedupe"] = str(e["ask_dedupe"])[:60]
    return clean, ""


def merge_registry(base, extra):
    """Pure. -> (registry, [problems]). `extra` entries are added to `base`, replace one with the same id, or remove it (disabled)."""
    by_id = {c["id"]: c for c in base}
    order = [c["id"] for c in base]
    problems = []
    for e in extra if isinstance(extra, list) else []:
        clean, bad = validate_entry(e)
        if bad:
            problems.append(bad)
            continue
        if clean.get("disabled"):
            by_id.pop(clean["id"], None)
            order = [i for i in order if i != clean["id"]]
            continue
        if clean["id"] not in by_id:
            order.append(clean["id"])
        by_id[clean["id"]] = clean
    return [by_id[i] for i in order if i in by_id], problems


def load_registry(logs=None):
    """-> (registry, [problems]). The built-in entries plus the user's logs/capabilities.json when it exists and parses."""
    logs = Path(logs) if logs else logs_dir()
    path = logs / REGISTRY_NAME
    try:
        if path.stat().st_size > MAX_REGISTRY:
            return list(DEFAULT_REGISTRY), ["%s is larger than %d KB and was ignored" % (REGISTRY_NAME, MAX_REGISTRY // 1024)]
        extra = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return list(DEFAULT_REGISTRY), []
    except (OSError, ValueError) as e:
        return list(DEFAULT_REGISTRY), ["%s cannot be read (%s), only the built-in entries are checked" % (REGISTRY_NAME, type(e).__name__)]
    if not isinstance(extra, list):
        return list(DEFAULT_REGISTRY), ["%s must hold a list of entries" % REGISTRY_NAME]
    return merge_registry(DEFAULT_REGISTRY, extra)


# ---------- impure probes ----------
_PROC_CACHE = {}


def _procs():
    """The running processes' command lines, looked up once per run (one lookup can cost a second or two). None when they cannot be listed."""
    if "v" not in _PROC_CACHE:
        import sense_health
        _PROC_CACHE["v"] = sense_health.process_commandlines()
    return _PROC_CACHE["v"]


def _task(name):
    import sense_health
    for t in sense_health.schtasks_rows(name=name):
        if t["name"].lower() == str(name).lstrip("\\").lower():
            return t
    return None


def probe(p):
    """-> (ok, detail). Never raises."""
    kind, arg = p.get("kind"), p.get("arg", "")
    try:
        if kind in WINDOWS_ONLY and sys.platform != "win32":
            return True, "not applicable here (Windows only)"
        if kind == "exists":
            ok = os.path.exists(resolve(arg, scripts_dir(), logs_dir(), vault_dir()))
            return ok, "present" if ok else "missing: " + arg
        if kind == "fresh":
            limit = p.get("minutes", 60)
            age = (time.time() - os.path.getmtime(resolve(arg, scripts_dir(), logs_dir(), vault_dir()))) / 60
            return age <= limit, "%d min old (limit %d)" % (age, limit)
        if kind == "http":
            import urllib.error
            import urllib.request
            if not str(arg).lower().startswith(("http://", "https://")):
                return False, "only http and https addresses can be probed"
            try:
                urllib.request.urlopen(arg, timeout=5).close()
            except urllib.error.HTTPError:
                pass                                      # any HTTP answer, even 401 or 404, means the service is up
            return True, "answers"
        if kind == "proc":
            lines = _procs()
            if lines is None:
                return True, "cannot list processes, assumed running"
            up = str(arg).lower() in "\n".join(lines).lower()
            return up, "running" if up else "not running"
        if kind == "which":
            import shutil
            found = shutil.which(arg)
            return bool(found), ("found " + os.path.basename(found)) if found else "not on PATH: " + arg
        if kind == "cred":
            try:
                import keyring
            except ImportError:
                return False, "the keyring package is not installed (pip install keyring)"
            have = bool(keyring.get_password(arg, p.get("user", "")))
            return have, "stored" if have else "no stored credential"
        if kind in WINDOWS_ONLY:
            row = _task(arg)
            if row is None:
                return False, "task not found: " + arg
            if kind == "task":
                return row["state"].lower() in ("ready", "running"), "state " + row["state"]
            return row["last_result"] in BENIGN, "last result " + row["last_result"]
        return False, "unknown probe kind %r" % kind
    except Exception as e:                                # noqa: BLE001 - a probe must never take the sensor down
        return False, "probe error: %s" % type(e).__name__


def _read_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def init_registry(logs=None):
    """Write the built-in entries to logs/capabilities.json for editing. -> (written, path). Never overwrites an existing file."""
    logs = Path(logs) if logs else logs_dir()
    path = logs / REGISTRY_NAME
    if path.exists():
        return False, path
    logs.mkdir(parents=True, exist_ok=True)
    _write_json(path, DEFAULT_REGISTRY)
    return True, path


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    logs = logs_dir()
    dry = "--dry-run" in argv
    if "--init" in argv:
        written, path = init_registry(logs)
        print(("wrote the starter registry to %s" % path) if written else "%s already exists, left as it is" % path)
        return 0
    if not dry and (logs / OFF_NAME).exists():
        print("sense_capabilities: off")
        return 0
    registry, problems = load_registry(logs)
    for p in problems:
        print("registry problem: " + p)
    _PROC_CACHE.clear()
    results = {c["id"]: probe(c["probe"]) for c in registry}
    state = _read_state(logs / STATE_NAME)
    week = time.strftime("%G-W%V")
    new_state, found = judge(registry, results, state, week)
    for c in registry:
        ok, detail = results[c["id"]]
        print("%-6s %-18s %s" % ("green" if ok else "RED", c["id"], detail))
    if dry:
        print("%d red, %d would raise now, nothing written" % (sum(1 for r in results.values() if not r[0]), len(found)))
        return 0
    import intent_store
    import notify
    logs.mkdir(parents=True, exist_ok=True)
    intents = intent_store.read_jsonl(intent_store.INTENTS, str(logs))
    added = 0
    for tier, value, what, dedupe in found:
        cand = intent_store.new_intent("capability_red", what, tier=tier, value=value, dedupe=dedupe, from_event="capability_red")
        if intent_store.is_duplicate(intents, cand):
            continue
        intent_store.append(intent_store.EVENTS, {"ts": intent_store.iso(), "source": "capabilities", "kind": "capability_red", "data": {"what": what[:200], "value": value}}, str(logs))
        res = notify.alert(what, source="sense_capabilities", intent_kind="capability_red", tier=tier, value=value, dedupe=dedupe, logs=str(logs))
        if res.get("intent"):
            added += 1
            intents.append(cand)
    _write_json(logs / STATE_NAME, new_state)
    print("sense_capabilities: %d finding(s), %d new intent(s)" % (len(found), added))
    return 0


if __name__ == "__main__":
    sys.exit(main())
