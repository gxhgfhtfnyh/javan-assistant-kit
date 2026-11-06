# -*- coding: utf-8 -*-
"""Defender exclusion check: did something tell Windows Defender to look away?

Windows only. Malware that has run once often adds its own folder, program or file type to the Defender exclusion lists so the antivirus stops scanning it. This
check does not guess whether an exclusion is bad: it remembers the three lists (paths, processes, extensions) and raises one ask-first work item (kind
defender_exclusion_added, tier 2, value 4) for every exclusion that is new since the last run. A removed exclusion is not an alarm.

The first run only records the lists (logs/defender_exclusion_baseline.json) and raises nothing. Every real run updates the lists afterwards, so one change alarms once.

Needs administrator rights: Windows hides the exclusion lists from an ordinary account (the answer says "N/A: Must be an administrator to view exclusions"). That
answer, a missing or switched-off Defender (another antivirus is the active one) and any failed query are reported as "unreadable": nothing is recorded as the
baseline, no work item is raised, and one line goes to logs/alerts.txt when the check turns from readable to unreadable. To use the check, run it (or the
scheduled job that runs it) from an elevated account.

    python defender_exclusion_check.py                 # compare with the remembered lists
    python defender_exclusion_check.py --rebaseline    # accept the current lists without alarms
    python defender_exclusion_check.py --dry-run       # print what would be reported; write nothing

Every real run appends one line to logs/defender_exclusion_check.jsonl. Kill switch: a file logs/defender_exclusion_check_off.txt. Off Windows it prints one line and
exits 0. The exit code is 0 whatever is found; it is 1 only when the script itself fails. Environment: HARNESS_ROOT, HARNESS_LOGS.
Pure core (unit tested): parse_exclusions, diff_exclusions, as_list."""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

NAME = "defender_exclusion_check"
SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

KEYS = ("paths", "processes", "extensions")
BASELINE_NAME = "defender_exclusion_baseline.json"
STATE_NAME = "defender_exclusion_state.json"
LOG_NAME = "defender_exclusion_check.jsonl"
OFF_NAME = "defender_exclusion_check_off.txt"
MAX_OUT = 1_000_000
MAX_ALERTS = 10
PS_SCRIPT = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
             "try { $p = Get-MpPreference -ErrorAction Stop } catch { exit 3 }; "
             "ConvertTo-Json -Compress @{ paths = $p.ExclusionPath; processes = $p.ExclusionProcess; extensions = $p.ExclusionExtension }")


def _logs():
    """The logs folder right now (the environment is read at call time so tests and parity runs can move it)."""
    root = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
    return Path(os.environ.get("HARNESS_LOGS") or root / "logs").resolve()


def run(args, timeout=30):
    """-> (exit code, stdout). 127 when the program does not exist, 124 on a time-out, 1 on any other failure. The one door for every system query; tests replace it."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout or "")[:MAX_OUT]
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""
    except Exception:
        return 1, ""


def clean(text, limit=200):
    """Pure. Text that came from outside, made safe for a log line or an alert: control characters become spaces, runs of spaces collapse, cut to `limit`."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(text)).split())[:limit]


def as_list(value):
    """Pure. PowerShell prints one item as a string, many as a list and none as null: all become a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def parse_exclusions(text):
    """Pure. -> ("ok", {"paths": [...], "processes": [...], "extensions": [...]}) | ("hidden", None) | ("bad", None).
    "hidden" is Windows refusing to show the lists to a non-administrator; "bad" is anything that is not the expected JSON."""
    try:
        data = json.loads(text or "")
    except ValueError:
        return "bad", None
    if not isinstance(data, dict):
        return "bad", None
    out = {k: as_list(data.get(k)) for k in KEYS}
    if any("must be an administrator" in v.lower() for k in KEYS for v in out[k]):
        return "hidden", None
    return "ok", out


def get_exclusions():
    """-> ("ok", lists) | ("hidden", None) | ("unavailable", None)."""
    code, out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT], timeout=30)
    if code != 0:
        return "unavailable", None
    status, data = parse_exclusions(out)
    return ("unavailable" if status == "bad" else status), data


def diff_exclusions(current, baseline):
    """Pure. -> {category: [new items]}. Windows paths, program names and extensions are not case sensitive, so the comparison is not either."""
    out = {}
    for k in KEYS:
        known = {v.lower() for v in as_list(baseline.get(k))}
        new = sorted(v for v in as_list(current.get(k)) if v.lower() not in known)
        if new:
            out[k] = new
    return out


def _read_json(path):
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


def _alert(text, kind=None, tier=2, value=4, dedupe=None):
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


def check(rebaseline=False, dry_run=False):
    """One check. -> {"timestamp", "status", "first_run", "new_exclusions", "total"}; status is ok, hidden or unavailable. Writes (unless dry_run) the baseline, a log line and the alerts."""
    logs = _logs()
    status, current = get_exclusions()
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    state_kind, state = _read_json(logs / STATE_NAME)
    if status != "ok":
        result = {"timestamp": stamp, "status": status, "first_run": False, "new_exclusions": {}, "total": {}}
        if not dry_run:
            if state.get("last_status") == "ok" or state_kind == "missing":
                what = ("Windows hides the Defender exclusion lists from this account; run the check as administrator" if status == "hidden"
                        else "the Defender exclusion lists cannot be read (Defender missing, switched off or the query failed)")
                _alert("defender exclusion check cannot do its job: " + what + ".")
            _write_json(logs / STATE_NAME, {"last_status": status, "updated": stamp})
            _append_log(logs, result)
        return result

    base_kind, base = _read_json(logs / BASELINE_NAME)
    first_run = base_kind == "missing"
    new = {} if (first_run or base_kind == "bad" or rebaseline) else diff_exclusions(current, base)
    result = {"timestamp": stamp, "status": "ok", "first_run": first_run, "new_exclusions": new, "total": {k: len(v) for k, v in current.items()},
              "baseline_unreadable": base_kind == "bad"}
    if dry_run:
        return result
    if base_kind == "bad" and not rebaseline:
        _alert("the Defender exclusion baseline (logs/%s) exists but cannot be read; it was rebuilt from the current lists. Ask the owner whether anything was changed." % BASELINE_NAME,
               kind="defender_exclusion_added", dedupe="defender-exclusion:baseline-unreadable")
    sent = 0
    for k in KEYS:
        for item in new.get(k, []):
            if sent >= MAX_ALERTS:
                break
            sent += 1
            digest = hashlib.sha1(item.lower().encode("utf-8", "replace")).hexdigest()[:12]
            _alert("new Defender exclusion (%s): %s. Ask the owner whether this was intended." % (k, clean(item)),
                   kind="defender_exclusion_added", tier=2, value=4, dedupe="defender-exclusion:%s:%s" % (k, digest))
    extra = sum(len(v) for v in new.values()) - sent
    if extra > 0:
        _alert("%d more new Defender exclusion(s) not listed here; see logs/%s" % (extra, LOG_NAME))
    _write_json(logs / BASELINE_NAME, current)
    _write_json(logs / STATE_NAME, {"last_status": "ok", "updated": stamp})
    _append_log(logs, result)
    return result


def _append_log(logs, result):
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / LOG_NAME, "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="defender_exclusion_check.py", description=__doc__.splitlines()[0])
    ap.add_argument("--rebaseline", action="store_true", help="accept the current exclusion lists without alarms")
    ap.add_argument("--dry-run", action="store_true", help="report only: write no baseline, log line or alert")
    args = ap.parse_args(argv)
    _utf8()
    if (_logs() / OFF_NAME).exists():
        print("defender_exclusion_check: off (logs/%s exists)" % OFF_NAME)
        return 0
    if sys.platform != "win32":
        print("defender_exclusion_check: Windows only (it reads the Windows Defender settings); nothing to do on this system")
        return 0
    try:
        r = check(rebaseline=args.rebaseline, dry_run=args.dry_run)
    except Exception as e:
        print("defender_exclusion_check: failed: %s: %s" % (type(e).__name__, clean(e)))
        return 1
    if r["status"] == "hidden":
        print("[UNREADABLE] Windows hides the exclusion lists from a non-administrator; run this check as administrator")
    elif r["status"] != "ok":
        print("[UNREADABLE] the Defender settings cannot be read (Defender missing, switched off or the query failed)")
    elif r["new_exclusions"]:
        print("[NEW EXCLUSION] %s" % json.dumps(r["new_exclusions"], ensure_ascii=False))
    elif r["first_run"]:
        print("[BASELINE_SET] %s%s" % (r["total"], " (dry run: nothing written)" if args.dry_run else ""))
    elif args.rebaseline:
        print("[REBASELINE] the current lists are accepted %s" % r["total"])
    else:
        print("[OK] no new Defender exclusions %s" % r["total"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
