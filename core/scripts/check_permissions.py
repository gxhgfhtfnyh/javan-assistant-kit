# -*- coding: utf-8 -*-
"""Permission drift check: did the access rules on the assistant's own folders change?

Windows only (it reads the folder permissions with the built-in icacls tool). Watches two folders: the vault (HARNESS_VAULT, default ROOT/vault) and the scripts folder
(ROOT/scripts). The first run records their access rules (logs/permissions_baseline.json) and raises nothing. Every later run compares; a change raises one ask-first
work item (kind permission_drift, tier 2, value 3) and one line in logs/alerts.txt. The check does not judge whether the baseline is good, it catches CHANGE from it.
The old baseline is kept while a drift is open (so it keeps showing as drift), and the same drift alarms only once; accept the new state with --rebaseline. A folder that
is missing or whose rules cannot be read this time is listed and never counts as drift. For a check of whether the folders grant write access to everyone, see
windows_hardening_audit.py.

    python check_permissions.py                 # compare with the remembered rules
    python check_permissions.py --rebaseline    # accept the current rules as the new baseline
    python check_permissions.py --dry-run       # print what would be reported; write nothing

Kill switch: a file logs/check_permissions_off.txt. Off Windows it prints one line and exits 0. The exit code is 0 whatever is found; it is 1 only when the script itself
fails. Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Pure core (unit tested): parse_icacls, diff_rules, clean."""
import argparse
import codecs
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

NAME = "check_permissions"
SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

BASELINE_NAME = "permissions_baseline.json"
ALARMED_NAME = "check_permissions_alarmed.json"
OFF_NAME = "check_permissions_off.txt"
MAX_OUT = 1_000_000
SHOWN = 3


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


def _vault():
    return Path(os.environ.get("HARNESS_VAULT") or _root() / "vault").resolve()


def watched():
    """-> [(label, folder)]: the folders this check watches, named by a label that stays the same if the folder moves."""
    return [("vault", _vault()), ("scripts", _root() / "scripts")]


def _oem():
    """icacls prints in the console's OEM code page; Python knows it as 'oem' on Windows."""
    try:
        codecs.lookup("oem")
        return "oem" if os.name == "nt" else "utf-8"
    except LookupError:
        return "utf-8"


def run(args, timeout=20):
    """-> (exit code, stdout). 127 when the program does not exist, 124 on a time-out, 1 on any other failure. The one door for every system query; tests replace it."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, encoding=_oem(), errors="replace", stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout or "")[:MAX_OUT]
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""
    except Exception:
        return 1, ""


def clean(text, limit=150):
    """Pure. Text that came from outside, made safe for a log line or an alert: control characters become spaces, runs of spaces collapse, cut to `limit`."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(text)).split())[:limit]


def parse_icacls(text, path=""):
    """Pure. The access rules in icacls output, one string per rule, in the order icacls prints them. The rules are the lines before the first blank line (the
    summary after it is in the display language and is dropped), and the folder name that icacls puts in front of the first rule is removed so a moved folder is not drift."""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            if lines:
                break
            continue
        lines.append(line)
    if lines and path:
        p = str(path)
        if lines[0].lower().startswith(p.lower()):
            lines[0] = lines[0][len(p):].strip()
    return [" ".join(l.split()) for l in lines if l.strip()]


def get_acl(path):
    """-> the folder's access rules, or None when icacls could not read them."""
    code, out = run(["icacls", str(path)])
    if code != 0:
        return None
    rules = parse_icacls(out, path)
    return rules or None


def diff_rules(old, new):
    """Pure. -> (added, removed) rule lists."""
    return [r for r in new if r not in old], [r for r in old if r not in new]


def _read_json(path):
    """-> ("missing" | "ok" | "bad", dict)"""
    try:
        if path.stat().st_size > MAX_OUT:
            return "bad", {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "missing", {}
    except (OSError, ValueError):
        return "bad", {}
    return ("ok", data) if isinstance(data, dict) else ("bad", {})


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _alert(text, kind=None, tier=2, value=3, dedupe=None):
    """Tell the owner through notify.alert (alerts.txt line, optional work item). Never raises."""
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source=NAME, intent_kind=kind, tier=tier, value=value, dedupe=dedupe, logs=_logs())
    except Exception:
        try:
            lg = _logs()
            lg.mkdir(parents=True, exist_ok=True)
            with open(lg / "alerts.txt", "a", encoding="utf-8") as f:
                f.write("[%s] %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), NAME, clean(text, 500)))
        except Exception:
            pass


def _drift_key(label, rules):
    return "%s@%s" % (label, hashlib.sha1("\n".join(rules).encode("utf-8", "replace")).hexdigest()[:12])


def check(rebaseline=False, dry_run=False):
    """One check. -> {"status": "baseline_set" | "clean" | "drift", "drift": {label: {"added", "removed"}}, "missing": [...], "unreadable": [...], "baseline_unreadable": bool, "labels": [...]}."""
    logs = _logs()
    current, missing, unreadable = {}, [], []
    for label, folder in watched():
        if not folder.exists():
            missing.append(label)
            continue
        rules = get_acl(folder)
        if rules is None:
            unreadable.append(label)
        else:
            current[label] = rules

    kind, data = _read_json(logs / BASELINE_NAME)
    old = data.get("paths") if isinstance(data.get("paths"), dict) else {}
    first_run, bad = kind == "missing", kind == "bad"
    accept_all = first_run or bad or rebaseline
    drift = {}
    if not accept_all:
        for label, rules in current.items():
            if isinstance(old.get(label), list) and old[label] != rules:
                added, removed = diff_rules(old[label], rules)
                drift[label] = {"added": added, "removed": removed}

    merged = dict(old) if kind == "ok" else {}
    for label, rules in current.items():
        if accept_all or label not in merged:                # a drifted label keeps its old baseline until the owner accepts the new state
            merged[label] = rules
    status = "drift" if drift else ("baseline_set" if (first_run or rebaseline) else "clean")
    result = {"status": status, "drift": drift, "missing": missing, "unreadable": unreadable, "baseline_unreadable": bad, "labels": sorted(current)}
    if dry_run:
        return result

    if bad and not rebaseline:
        _alert("the permissions baseline (logs/%s) exists but cannot be read; it was rebuilt from the current rules. Ask the owner whether anything was changed." % BASELINE_NAME,
               kind="permission_drift", dedupe="permissions:baseline-unreadable")
    a_kind, a_data = _read_json(logs / ALARMED_NAME)
    alarmed = set(a_data.get("alarmed", [])) if (a_kind == "ok" and not rebaseline) else set()
    still = set()
    for label, d in drift.items():
        key = _drift_key(label, current[label])
        still.add(key)
        if key in alarmed:
            continue
        shown = "; ".join(["added " + clean(r) for r in d["added"][:SHOWN]] + ["removed " + clean(r) for r in d["removed"][:SHOWN]])
        _alert("permissions changed on the %s folder (%d rule(s) added, %d removed): %s. Ask the owner whether this was intended; if yes run check_permissions.py --rebaseline."
               % (label, len(d["added"]), len(d["removed"]), shown), kind="permission_drift", tier=2, value=3, dedupe="permdrift:" + key)
    if merged != old or kind != "ok":
        _write_json(logs / BASELINE_NAME, {"paths": merged})
    if still != alarmed:
        _write_json(logs / ALARMED_NAME, {"alarmed": sorted(still)})
    return result


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="check_permissions.py", description=__doc__.splitlines()[0])
    ap.add_argument("--rebaseline", action="store_true", help="accept the current access rules as the new baseline")
    ap.add_argument("--dry-run", action="store_true", help="report only: write no baseline, state or alert")
    args = ap.parse_args(argv)
    _utf8()
    if (_logs() / OFF_NAME).exists():
        print("check_permissions: off (logs/%s exists)" % OFF_NAME)
        return 0
    if sys.platform != "win32":
        print("check_permissions: Windows only (it reads folder permissions with icacls); nothing to do on this system")
        return 0
    try:
        r = check(rebaseline=args.rebaseline, dry_run=args.dry_run)
    except Exception as e:
        print("check_permissions: failed: %s: %s" % (type(e).__name__, clean(e, 200)))
        return 1
    for label in r["missing"]:
        print("[MISSING] the %s folder does not exist; not checked" % label)
    for label in r["unreadable"]:
        print("[UNREADABLE] the access rules of the %s folder could not be read; not checked" % label)
    if r["baseline_unreadable"]:
        print("[BASELINE UNREADABLE] the old rules could not be read" + ("" if args.dry_run else " and were rebuilt"))
    if r["status"] == "drift":
        print("PERMISSION DRIFT DETECTED:")
        for label, d in r["drift"].items():
            print("  %s folder" % label)
            for rule in d["added"]:
                print("    + %s" % clean(rule, 300))
            for rule in d["removed"]:
                print("    - %s" % clean(rule, 300))
    elif r["status"] == "baseline_set":
        print("[BASELINE_SET] %s%s" % (", ".join(r["labels"]) or "nothing readable", " (dry run: nothing written)" if args.dry_run else ""))
    else:
        print("[OK] no permission drift (%s)" % (", ".join(r["labels"]) or "nothing readable"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
