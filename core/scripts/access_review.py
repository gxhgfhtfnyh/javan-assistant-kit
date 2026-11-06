# -*- coding: utf-8 -*-
"""Access review: did someone get added to a powerful local group?

Windows only. Lists the members of the local Administrators group and of the local Remote Desktop Users group, remembers the lists, and raises one
ask-first work item (kind privileged_group_change, tier 2, value 4) for every account that is new since the last run. A removed member is not an alarm.
The groups are found by their fixed Windows ids (S-1-5-32-544 and S-1-5-32-555), so the check works on any display language.

The first run only records the lists (logs/access_review_baseline.json) and raises nothing. Every real run updates the lists afterwards, so one change alarms
once. A group that cannot be read this time (PowerShell missing, a stale account inside the group, no permission) is reported and left as it was: it is never
recorded as empty, because the next good read would then flag every member as new. A baseline file that exists but cannot be read is itself reported once.

    python access_review.py                 # compare with the remembered lists
    python access_review.py --rebaseline    # accept the current lists without alarms
    python access_review.py --dry-run       # print what would be reported; write nothing

Every real run appends one line to logs/access_review.jsonl. Kill switch: a file logs/access_review_off.txt. Off Windows it prints one line and exits 0.
The exit code is 0 whatever is found (the alarm is the signal); it is 1 only when the script itself fails.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): parse_members, group_script, diff_members, clean."""
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

NAME = "access_review"
SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

GROUPS = {"Administrators": "S-1-5-32-544", "Remote Desktop Users": "S-1-5-32-555"}
BASELINE_NAME = "access_review_baseline.json"
LOG_NAME = "access_review.jsonl"
OFF_NAME = "access_review_off.txt"
SID_RE = re.compile(r"^S-1-\d+(-\d+)+$")
MAX_OUT = 1_000_000
MAX_ALERTS = 10


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


def clean(text, limit=120):
    """Pure. A name that came from outside, made safe for a log line or an alert: control characters become spaces, runs of spaces collapse, cut to `limit`."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(text)).split())[:limit]


def group_script(sid):
    """Pure. The PowerShell text that prints the member names of the local group with this well-known id, one per line; it exits 3 when the group cannot be read."""
    if not SID_RE.match(str(sid)):
        raise ValueError("not a Windows security id: %r" % (sid,))
    return ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "try { (Get-LocalGroupMember -SID '%s' -ErrorAction Stop).Name } catch { exit 3 }" % sid)


def parse_members(text):
    """Pure. Sorted unique member names from the PowerShell output (one name per line)."""
    return sorted({line.strip() for line in (text or "").splitlines() if line.strip()})


def group_members(sid):
    """-> sorted member names, [] for a group that is really empty, or None when the group could not be read."""
    code, out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", group_script(sid)], timeout=30)
    if code != 0:
        return None
    return parse_members(out)


def read_groups():
    """-> {group name: [members] or None}"""
    return {name: group_members(sid) for name, sid in GROUPS.items()}


def diff_members(current, baseline_groups):
    """Pure. -> {group: [new members]} for groups that are readable now and were recorded before. A group not yet recorded is not compared (it is recorded silently)."""
    out = {}
    for group, members in current.items():
        if members is None or group not in baseline_groups:
            continue
        new = sorted(set(members) - set(str(m) for m in baseline_groups[group]))
        if new:
            out[group] = new
    return out


def _read_json(path):
    """-> ("missing" | "ok" | "bad", dict). A file that is there but is not a json object (or is huge) is "bad"."""
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
    """One review. -> {"timestamp", "first_run", "groups", "new_members", "unreadable", "baseline_unreadable"}. Writes (unless dry_run) the baseline, one log line and the alerts."""
    logs = _logs()
    current = read_groups()
    unreadable = sorted(g for g, m in current.items() if m is None)
    state, data = _read_json(logs / BASELINE_NAME)
    old = data.get("groups") if isinstance(data.get("groups"), dict) else {}
    first_run = state == "missing"
    bad = state == "bad"
    new_members = {} if (first_run or bad or rebaseline) else diff_members(current, old)

    merged = {g: m for g, m in current.items() if m is not None}
    for g in unreadable:                                   # keep what we knew about a group we could not read now
        if g in old:
            merged[g] = old[g]
    result = {"timestamp": datetime.datetime.now().isoformat(timespec="seconds"), "first_run": first_run, "groups": current,
              "new_members": new_members, "unreadable": unreadable, "baseline_unreadable": bad}
    if dry_run:
        return result

    if bad and not rebaseline:
        _alert("the access review baseline (logs/%s) exists but cannot be read; it was rebuilt from the current group lists. Ask the owner whether anything was changed." % BASELINE_NAME,
               kind="privileged_group_change", dedupe="access_review:baseline-unreadable")
    sent = 0
    for group, members in new_members.items():
        for member in members:
            if sent >= MAX_ALERTS:
                break
            sent += 1
            key = hashlib.sha1(member.lower().encode("utf-8", "replace")).hexdigest()[:12]
            _alert("new member of the local %s group: %s. Ask the owner whether this was intended." % (group, clean(member)),
                   kind="privileged_group_change", tier=2, value=4, dedupe="access_review:%s:%s" % (group, key))
    extra = sum(len(v) for v in new_members.values()) - sent
    if extra > 0:
        _alert("%d more new privileged group member(s) not listed here; see logs/%s" % (extra, LOG_NAME))
    _write_json(logs / BASELINE_NAME, {"groups": merged, "updated": result["timestamp"]})
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / LOG_NAME, "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="access_review.py", description=__doc__.splitlines()[0])
    ap.add_argument("--rebaseline", action="store_true", help="accept the current member lists without alarms")
    ap.add_argument("--dry-run", action="store_true", help="report only: write no baseline, log line or alert")
    args = ap.parse_args(argv)
    _utf8()
    if (_logs() / OFF_NAME).exists():
        print("access_review: off (logs/%s exists)" % OFF_NAME)
        return 0
    if sys.platform != "win32":
        print("access_review: Windows only (it reads the local Windows groups); nothing to do on this system")
        return 0
    try:
        r = check(rebaseline=args.rebaseline, dry_run=args.dry_run)
    except Exception as e:
        print("access_review: failed: %s: %s" % (type(e).__name__, clean(e, 200)))
        return 1
    for group, members in r["groups"].items():
        print("%s: %s" % (group, "unreadable" if members is None else "%d member(s)" % len(members)))
    if r["baseline_unreadable"]:
        print("[BASELINE UNREADABLE] the old list could not be read" + ("" if args.dry_run else " and was rebuilt"))
    if r["new_members"]:
        for group, members in r["new_members"].items():
            print("[NEW MEMBER] %s: %s" % (group, ", ".join(clean(m) for m in members)))
    elif r["first_run"]:
        print("[BASELINE_SET]" + (" (dry run: nothing written)" if args.dry_run else " the current lists are recorded"))
    elif args.rebaseline:
        print("[REBASELINE] the current lists are accepted")
    elif r["unreadable"]:
        print("[UNREADABLE] %s could not be read; its old list was kept" % ", ".join(r["unreadable"]))
    else:
        print("[OK] no new members since the last check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
