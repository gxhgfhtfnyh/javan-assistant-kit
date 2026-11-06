# -*- coding: utf-8 -*-
"""Disk space check: is the disk the assistant lives on running out of room?

Checks the free space, as a PERCENTAGE of each disk (a flat gigabyte floor is meaningless for a small or fixed-size volume), of the disks that
hold: the assistant folder, its logs, its vault and the user's home folder. Two paths on the same disk are reported once. Extra places to
watch go one per line in logs/disk_space_paths.txt (lines starting with # are comments) or on the command line with --path.

    python disk_space_check.py                    # print one line per disk; exit 1 when a disk is low
    python disk_space_check.py --job              # for the scheduler: same, but always exit 0 (a low disk is reported through an alert, not a failure)
    python disk_space_check.py --warn-percent 10  # flag a disk with less than 10 percent free (default 15)
    python disk_space_check.py --min-free-gb 2    # also flag a disk with less than 2 GB free (default 0 = off)
    python disk_space_check.py --no-alert         # print only; write nothing

A low disk writes one line to logs/alerts.txt and raises one work item (kind disk_low, via notify.py). The same disk alerts again at most
once a day (state in logs/disk_space_state.json). Nothing is ever deleted here: cleanup_scratch.py is the tool for that, and it asks first.
Kill switch: a file logs/disk_space_check_off.txt makes --job do nothing.
Paths: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Pure core (unit tested): check_paths, due_for_alert, format_line."""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = "disk_space_check"
OFF_NAME = "disk_space_check_off.txt"
PATHS_NAME = "disk_space_paths.txt"
STATE_NAME = "disk_space_state.json"
WARN_PERCENT = 15.0
REALERT_SEC = 24 * 3600
MAX_CONFIG_LINES = 50


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or (_root() / "logs")).resolve()


def _vault():
    return Path(os.environ.get("HARNESS_VAULT") or (_root() / "vault")).resolve()


def _home():
    return Path.home()


def nearest_existing(path):
    """The path itself, or its closest parent that exists (a vault folder not made yet still lives on some disk). None when nothing exists."""
    p = Path(path)
    for cand in [p] + list(p.parents):
        try:
            if cand.exists():
                return cand
        except OSError:
            continue
    return None


def device_key(path):
    """What identifies the disk a path is on: the device number from the operating system, else the drive or root of the path."""
    try:
        return "dev:%s" % os.stat(str(path)).st_dev
    except OSError:
        return "root:%s" % (Path(path).anchor or str(path))


def configured_paths(logs=None):
    """Extra paths from logs/disk_space_paths.txt: one per line, blank lines and # comments skipped, at most 50 lines."""
    p = (Path(logs) if logs else _logs()) / PATHS_NAME
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines()[:MAX_CONFIG_LINES]:
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def default_paths():
    """The places that matter by default: the assistant folder, its logs, its vault and the home folder (only those that exist)."""
    out = []
    for p in (_root(), _logs(), _vault(), _home()):
        e = nearest_existing(p)
        if e is not None and str(e) not in out:
            out.append(str(e))
    return out


def check_paths(paths, warn_percent=WARN_PERCENT, min_free_gb=0.0, usage=None, devkey=None):
    """Pure core (usage and devkey can be faked). -> [{"path", "key", "free_gb", "total_gb", "percent_free", "low", "why"}], one row per DISK:
    paths on the same disk collapse into the first one given; a path that cannot be read, or a disk of size 0, is skipped."""
    usage = usage or shutil.disk_usage
    devkey = devkey or device_key
    seen, rows = set(), []
    for path in paths:
        key = devkey(path)
        if key in seen:
            continue
        try:
            total, _used, free = usage(str(path))
        except (OSError, ValueError):
            continue
        if not total:
            continue
        seen.add(key)
        percent = free / total * 100.0
        free_gb = free / (1024 ** 3)
        why = ""
        if percent < warn_percent:
            why = "under %g percent free" % warn_percent
        elif min_free_gb and free_gb < min_free_gb:
            why = "under %g GB free" % min_free_gb
        rows.append({"path": str(path), "key": key, "free_gb": round(free_gb, 1), "total_gb": round(total / (1024 ** 3), 1),
                     "percent_free": round(percent, 1), "low": bool(why), "why": why})
    return rows


def format_line(r):
    return "[%s] %s: %s GB free of %s GB (%s%%)%s" % ("LOW" if r["low"] else "ok", r["path"], r["free_gb"], r["total_gb"], r["percent_free"],
                                                        " - " + r["why"] if r["why"] else "")


def due_for_alert(state, key, now, cooldown=REALERT_SEC):
    """True when this disk has not been alerted about in the last `cooldown` seconds."""
    try:
        marks = state.get("alerted")
        if not isinstance(marks, dict) or key not in marks:
            return True
        return now - float(marks[key]) >= cooldown
    except (TypeError, ValueError, AttributeError):
        return True


def _load_state(logs):
    try:
        data = json.loads((Path(logs) / STATE_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(logs, state):
    p = Path(logs) / STATE_NAME
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(str(tmp), str(p))
    except OSError:
        pass


def _alert(text, key):
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import notify
        notify.alert(text, source=SOURCE, intent_kind="disk_low", tier=1, value=3, dedupe="disk-low-" + key, logs=_logs())
        return True
    except Exception:
        return False


def run(paths=None, warn_percent=WARN_PERCENT, min_free_gb=0.0, alert=True, now=None, usage=None, devkey=None):
    """-> (rows, alerted_paths). Alerts for each low disk not alerted about in the last day."""
    now = time.time() if now is None else now
    if paths is None:
        paths = default_paths() + configured_paths()
    rows = check_paths(paths, warn_percent, min_free_gb, usage=usage, devkey=devkey)
    alerted = []
    if alert and any(r["low"] for r in rows):
        logs = _logs()
        state = _load_state(logs)
        marks = state.get("alerted") if isinstance(state.get("alerted"), dict) else {}
        for r in rows:
            if r["low"] and due_for_alert({"alerted": marks}, r["key"], now):
                text = "Disk low: %s has %s GB free of %s GB (%s%%), %s. Free some space (cleanup_scratch.py shows what old scratch files it could remove)." % (
                    r["path"], r["free_gb"], r["total_gb"], r["percent_free"], r["why"])
                if _alert(text, r["key"]):
                    marks[r["key"]] = now
                    alerted.append(r["path"])
        state["alerted"] = marks
        _save_state(logs, state)
    return rows, alerted


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="warn when a disk is nearly full")
    ap.add_argument("--path", action="append", default=[], help="an extra path to check (repeatable)")
    ap.add_argument("--warn-percent", type=float, default=WARN_PERCENT)
    ap.add_argument("--min-free-gb", type=float, default=0.0)
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--job", action="store_true", help="always exit 0; honour logs/disk_space_check_off.txt")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if not (0 <= a.warn_percent <= 100) or a.min_free_gb < 0:
        print("bad threshold: --warn-percent must be 0..100 and --min-free-gb 0 or more")
        return 2
    if a.job and (_logs() / OFF_NAME).exists():
        print("disk_space_check: off")
        return 0
    paths = default_paths() + configured_paths() + list(a.path)
    rows, _alerted = run(paths, a.warn_percent, a.min_free_gb, alert=not a.no_alert)
    for r in rows:
        print(format_line(r))
    if not rows:
        print("no disk could be read")
    low = any(r["low"] for r in rows)
    return 0 if (a.job or not low) else 1


if __name__ == "__main__":
    sys.exit(main())
