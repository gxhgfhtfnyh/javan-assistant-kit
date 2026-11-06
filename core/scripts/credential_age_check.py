# -*- coding: utf-8 -*-
"""Tracks how long it has been since each credential was last confirmed present, and flags old ones for a rotation look.

The OS keychain (the `keyring` package) keeps no "date set", so this keeps its own sidecar record, logs/credential_ages.json:
    {"my-service/me": "2026-10-31"}
Nothing records for you: run the `record` command whenever you set, rotate or check a credential (a setup script or a smoke test can call
record_confirmed(service, username) from Python instead). No secret value is ever read or stored here, only names and a date.

Usage:
    python credential_age_check.py                          print the age report (default threshold 180 days; --days N changes it)
    python credential_age_check.py record SERVICE USERNAME  note that this credential was confirmed today
    python credential_age_check.py --job                    for a timer: one alert line when credentials are stale (at most weekly, or when the set changes); exits 0
From Python: record_confirmed(service, username), check_ages(stale_days=180) -> [(key, age_days)].
Kill switch (for --job): logs/credential_age_check_off.txt.  Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): clean_name, load_records, record_confirmed, check_ages, report_lines, should_alert."""
import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

RECORD_NAME = "credential_ages.json"
STATE_NAME = "credential_age_state.json"
OFF_NAME = "credential_age_check_off.txt"
STALE_DAYS = 180
MAX_NAME = 200
MAX_RECORD_BYTES = 2_000_000
ALERT_EVERY_S = 7 * 24 * 3600


def record_path():
    return Path(LOGS) / RECORD_NAME


def clean_name(value):
    """A service or user name safe to store: stripped, no control characters, 1..200 long. Raises ValueError otherwise."""
    s = str(value if value is not None else "").strip()
    if not s or len(s) > MAX_NAME or any(not ch.isprintable() for ch in s):
        raise ValueError("a name must be 1-%d printable characters" % MAX_NAME)
    return s


def _write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_records(path=None):
    """-> (records dict, corrupt bool). A missing file is ({}, False). A file that does not parse, or is too big, is ({}, True)."""
    p = Path(path) if path else record_path()
    try:
        if p.stat().st_size > MAX_RECORD_BYTES:
            return {}, True
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError):
        return {}, True
    if not isinstance(data, dict):
        return {}, True
    return {str(k): str(v) for k, v in data.items()}, False


def record_confirmed(service, username, today=None):
    """Note that service/username was confirmed present today. A record file that does not parse is moved aside (.corrupt-<time>) and a fresh one started."""
    key = "%s/%s" % (clean_name(service), clean_name(username))
    p = record_path()
    records, corrupt = load_records(p)
    if corrupt:
        try:
            os.replace(p, p.with_name(p.name + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S")))
        except OSError:
            pass
    records[key] = (today or dt.date.today()).isoformat()
    _write_atomic(p, json.dumps(records, indent=2, sort_keys=True))
    return key


def check_ages(stale_days=STALE_DAYS, today=None, records=None):
    """-> [(key, age_days)] older than stale_days, oldest first. A record with a bad date is skipped."""
    today = today or dt.date.today()
    if records is None:
        records, _ = load_records()
    stale = []
    for key, date_str in records.items():
        try:
            age = (today - dt.date.fromisoformat(date_str)).days
        except ValueError:
            continue
        if age > stale_days:
            stale.append((key, age))
    return sorted(stale, key=lambda t: (-t[1], t[0]))


def report_lines(records, stale_days=STALE_DAYS, today=None):
    today = today or dt.date.today()
    lines = ["=== Credential Age Report (%d tracked) ===" % len(records), ""]
    for key, date_str in sorted(records.items()):
        try:
            age = (today - dt.date.fromisoformat(date_str)).days
        except ValueError:
            lines.append("%s: unreadable date %r" % (key, date_str[:20]))
            continue
        flag = " <- STALE, consider rotating" if age > stale_days else ""
        lines.append("%s: last confirmed %s (%dd ago)%s" % (key, date_str, age, flag))
    return lines


def should_alert(stale_keys, state, now=None):
    """Pure. Alert when there is something stale AND (the set of stale credentials changed OR a week passed since the last alert)."""
    now = time.time() if now is None else now
    if not stale_keys:
        return False
    if sorted(stale_keys) != sorted(state.get("keys", [])):
        return True
    return now - float(state.get("ts", 0) or 0) >= ALERT_EVERY_S


def _load_state():
    try:
        d = json.loads((Path(LOGS) / STATE_NAME).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _alert(text):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source="credential_age_check", intent_kind="credential_rotation", tier=2, value=2, dedupe="credential-age", logs=str(LOGS))
    except Exception:
        pass


def run_job(stale_days=STALE_DAYS, now=None):
    """The timed pass. -> the number of stale credentials. Silent when nothing is recorded or nothing is stale."""
    stale = check_ages(stale_days)
    keys = [k for k, _ in stale]
    state = _load_state()
    if should_alert(keys, state, now):
        _alert("%d credential(s) not confirmed in %d+ days, consider rotating: %s" % (len(keys), stale_days, ", ".join("%s (%dd)" % t for t in stale[:10])))
        try:
            _write_atomic(Path(LOGS) / STATE_NAME, json.dumps({"keys": keys, "ts": time.time() if now is None else now}))
        except OSError:
            pass
    return len(stale)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="credential age report and record")
    ap.add_argument("command", nargs="*", help="record SERVICE USERNAME (omit to print the report)")
    ap.add_argument("--days", type=int, default=STALE_DAYS)
    ap.add_argument("--job", action="store_true")
    a = ap.parse_args(argv)
    if a.job:
        if (Path(LOGS) / OFF_NAME).exists():
            print("credential_age_check: off")
            return 0
        try:
            n = run_job(a.days)
        except Exception as e:
            print("credential_age_check: %s" % type(e).__name__)
            return 0
        print("credential_age_check: %d stale" % n)
        return 0
    if a.command:
        if a.command[0] != "record" or len(a.command) != 3:
            print("usage: credential_age_check.py record SERVICE USERNAME")
            return 2
        try:
            key = record_confirmed(a.command[1], a.command[2])
        except ValueError as e:
            print("not recorded: %s" % e)
            return 2
        except OSError as e:
            print("not recorded: %s" % type(e).__name__)
            return 1
        print("recorded %s as confirmed today" % key)
        return 0
    records, corrupt = load_records()
    if corrupt:
        print("The record file logs/%s could not be read; the next 'record' will move it aside and start a new one." % RECORD_NAME)
        return 0
    if not records:
        print("No credential-age records yet. Run: python credential_age_check.py record SERVICE USERNAME")
        return 0
    print("\n".join(report_lines(records, a.days)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
