# -*- coding: utf-8 -*-
"""Backup locations check: do your backups live on more than one physical disk, and are they recent?

A backup that sits on the same disk as the thing it protects is lost together with it. This keeps that gap visible instead of assumed handled. It cannot fix
it: a real second location (an external drive, a cloud-synced folder, another machine) is the owner's decision. It only reads folders and file dates.

Tell it where your backups go in logs/backup_locations.txt, one folder per line, optionally with a file pattern after a bar (default: any file):

    # folder [| file pattern]
    /Volumes/ExternalDrive/assistant-backups | *.zip
    ~/CloudDrive/assistant-backups

A folder counts as a location only when it exists and holds at least one matching file. Folders on the SAME physical disk count once. The check is
flagged when fewer than two different disks hold a backup, when every backup is on the same disk as the vault, or when a location's newest file is older
than --max-age-days (default 14; 0 turns the age check off).

    python check_backup_locations.py                 # print the result; exit 1 when something is flagged
    python check_backup_locations.py --job           # for the scheduler: always exit 0; honours logs/check_backup_locations_off.txt
    python check_backup_locations.py --folder DIR    # one more folder for this run (repeatable)
    python check_backup_locations.py --no-alert

A flag writes one alert line and raises one tier-2 work item (kind backup_gap, a decision for the owner), repeated at most weekly while the problem stays the same.
Paths: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Pure core (unit tested): parse_locations, assess."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = "check_backup_locations"
OFF_NAME = "check_backup_locations_off.txt"
CONFIG_NAME = "backup_locations.txt"
STATE_NAME = "backup_locations_state.json"
DEFAULT_PATTERN = "*"
MAX_AGE_DAYS = 14
REALERT_SEC = 7 * 24 * 3600
MAX_LINES = 30
MAX_SCAN = 5000


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or (_root() / "logs")).resolve()


def _vault():
    return Path(os.environ.get("HARNESS_VAULT") or (_root() / "vault")).resolve()


def device_key(path):
    """The physical disk a path is on (device number from the operating system; else the root of the path)."""
    try:
        return "dev:%s" % os.stat(str(path)).st_dev
    except OSError:
        return "root:%s" % (Path(path).anchor or str(path))


def parse_locations(text):
    """Pure. Config text -> [(folder, pattern)]. Blank lines and # comments are skipped; at most 30 lines; ~ means the home folder."""
    out = []
    for line in (text or "").splitlines()[:MAX_LINES]:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        folder, _, pattern = line.partition("|")
        folder = folder.strip()
        if folder:
            out.append((os.path.expanduser(folder), pattern.strip() or DEFAULT_PATTERN))
    return out


def load_locations(logs=None):
    p = (Path(logs) if logs else _logs()) / CONFIG_NAME
    try:
        return parse_locations(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def scan_location(folder, pattern):
    """-> {"folder", "files", "newest"} for a folder, or None when it is missing, unreadable, or holds no file that matches the pattern."""
    p = Path(folder)
    try:
        if not p.is_dir():
            return None
        files, newest = 0, 0.0
        for i, f in enumerate(p.glob(pattern)):
            if i >= MAX_SCAN:
                break
            try:
                if f.is_file():
                    files += 1
                    newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
    except (OSError, ValueError, NotImplementedError):
        return None
    return {"folder": str(p), "files": files, "newest": newest} if files else None


def assess(locations, vault_key=None, now=None, max_age_days=MAX_AGE_DAYS, devkey=None):
    """Pure core (devkey can be faked). `locations` are scan_location results. ->
    {"location_count", "disk_count", "locations", "same_disk_as_vault", "stale", "single_point_of_failure", "problems"}."""
    now = time.time() if now is None else now
    devkey = devkey or device_key
    disks = {}
    for loc in locations:
        disks.setdefault(devkey(loc["folder"]), []).append(loc["folder"])
    problems = []
    if not locations:
        problems.append("no backup found: no folder in %s holds a backup yet" % CONFIG_NAME)
    elif len(disks) == 1:
        problems.append("backups exist on only one disk (%s): if it fails, they go with it" % ", ".join(sorted(next(iter(disks.values())))))
    same = bool(locations) and vault_key is not None and set(disks) == {vault_key}
    if same:
        problems.append("every backup is on the same disk as the vault: a failure of that disk loses both")
    stale = []
    if max_age_days and max_age_days > 0:
        for loc in locations:
            if now - loc["newest"] > max_age_days * 86400:
                stale.append(loc["folder"])
        if stale:
            problems.append("backups older than %d days in: %s" % (max_age_days, ", ".join(stale)))
    return {"location_count": len(locations), "disk_count": len(disks), "locations": [l["folder"] for l in locations], "same_disk_as_vault": same,
            "stale": stale, "single_point_of_failure": len(disks) <= 1, "problems": problems}


def check(extra_folders=(), now=None, max_age_days=MAX_AGE_DAYS, devkey=None):
    configured = load_locations() + [(os.path.expanduser(str(f)), DEFAULT_PATTERN) for f in extra_folders]
    seen, found = set(), []
    for folder, pattern in configured:
        key = (os.path.realpath(folder), pattern)
        if key in seen:
            continue
        seen.add(key)
        loc = scan_location(folder, pattern)
        if loc:
            found.append(loc)
    vault = _vault()
    probe = vault
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return assess(found, (devkey or device_key)(probe), now, max_age_days, devkey)


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


def maybe_alert(result, now=None):
    """Alert when something is flagged and the same problem was not alerted about in the last week. -> True when an alert was written."""
    now = time.time() if now is None else now
    if not result["problems"]:
        return False
    logs = _logs()
    state = _load_state(logs)
    sig = " | ".join(result["problems"])
    try:
        if state.get("sig") == sig and now - float(state.get("ts", 0)) < REALERT_SEC:
            return False
    except (TypeError, ValueError):
        pass
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import notify
        notify.alert("Backups: " + "; ".join(result["problems"]) + ". A second location is your decision (external drive, cloud-synced folder, another machine).",
                     source=SOURCE, intent_kind="backup_gap", tier=2, value=2, dedupe="backup-gap", logs=logs)
    except Exception:
        return False
    _save_state(logs, {"sig": sig, "ts": now})
    return True


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="are the backups on more than one disk, and recent?")
    ap.add_argument("--folder", action="append", default=[])
    ap.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS)
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--job", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if a.max_age_days < 0:
        print("bad value: --max-age-days must be 0 or more")
        return 2
    if a.job and (_logs() / OFF_NAME).exists():
        print("check_backup_locations: off")
        return 0
    r = check(a.folder, max_age_days=a.max_age_days)
    if r["problems"]:
        print("REMINDER: %d backup location(s) on %d disk(s): %s" % (r["location_count"], r["disk_count"], r["locations"]))
        for p in r["problems"]:
            print("  - " + p)
        print("Needs a real decision (external drive, cloud-synced folder, another machine); this script does not set one up.")
        if not a.no_alert:
            maybe_alert(r)
    else:
        print("OK: backups exist on %d disks%s: %s" % (r["disk_count"], ", newest within %d days" % a.max_age_days if a.max_age_days else "", r["locations"]))
    return 0 if (a.job or not r["problems"]) else 1


if __name__ == "__main__":
    sys.exit(main())
