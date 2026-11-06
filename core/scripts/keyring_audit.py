# -*- coding: utf-8 -*-
"""Lists the assistant's credentials in the Windows Credential Manager and how long ago each was last looked up through `audited_keyring`,
and flags the ones unused for more than 90 days as candidates for rotation or removal. Windows only: elsewhere it prints one line and exits 0.

The Credential Manager is a machine-wide store that also holds the user's unrelated personal logins, so this reports ONLY the credentials in scope:
  * a service name that starts with the prefix (default "assistant-"), or
  * a service listed in logs/credential_services.json:
        {"prefix": "assistant-", "services": [{"service": "my-api", "username": "me", "revoke_url": "https://..."}, "other-service"]}
    ("prefix": "" turns the prefix off; a plain string entry means any username; "revoke_url" is used by break_glass.py.)
Nothing else in the store is read or reported. No secret value is ever read: the audit uses `cmdkey /list` (names only) and the access trail written
by audited_keyring.py (logs/keyring_access.jsonl).

Usage:
    python keyring_audit.py          print the report
    python keyring_audit.py --job    for a timer: one alert line (logs/alerts.txt) when something is stale; always exits 0
Kill switch (for --job): logs/keyring_audit_off.txt.  Limit: `cmdkey /list` prints English labels, so a non-English Windows may list nothing.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): load_scope, in_scope, parse_cmdkey, last_access_dates, build_report."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

SERVICES_FILE = "credential_services.json"
ACCESS_NAME = "keyring_access.jsonl"
OFF_NAME = "keyring_audit_off.txt"
DEFAULT_PREFIX = "assistant-"
STALE_DAYS = 90
MAX_READ_BYTES = 5_000_000
MAX_CONFIG_BYTES = 200_000


def load_scope(logs=None):
    """-> {"prefix": str, "services": [{"service", "username"?, "revoke_url"?}]}. A missing or unreadable config means the default prefix and no list."""
    scope = {"prefix": DEFAULT_PREFIX, "services": []}
    p = Path(logs or LOGS) / SERVICES_FILE
    try:
        if p.stat().st_size > MAX_CONFIG_BYTES:
            return scope
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return scope
    if not isinstance(data, dict):
        return scope
    if isinstance(data.get("prefix"), str):
        scope["prefix"] = data["prefix"]
    items = data.get("services")
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str) and item.strip():
            scope["services"].append({"service": item.strip()})
        elif isinstance(item, dict) and isinstance(item.get("service"), str) and item["service"].strip():
            row = {"service": item["service"].strip()}
            for k in ("username", "revoke_url"):
                if isinstance(item.get(k), str) and item[k].strip():
                    row[k] = item[k].strip()
            scope["services"].append(row)
    return scope


def in_scope(service, username, scope):
    """True when this credential is one the assistant owns: it matches the prefix or a listed service (and its username, when the list gives one)."""
    if not service:
        return False
    prefix = scope.get("prefix") or ""
    if prefix and service.startswith(prefix):
        return True
    for row in scope.get("services", []):
        if row["service"] == service and (not row.get("username") or row["username"] == username):
            return True
    return False


def parse_cmdkey(text):
    """-> [(service, username)] from `cmdkey /list` output. The real 'User:' line under each Target is used rather than guessing the user from the
    target string, because the Windows keyring backend stores the same (service, user) either as 'service' or as 'user@service'."""
    lines = (text or "").splitlines()
    pairs = []
    for i, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("Target: LegacyGeneric:target="):
            continue
        target = line.split("target=", 1)[1]
        service = target.split("@", 1)[1] if "@" in target else target
        username = None
        for look in lines[i + 1:i + 4]:      # User: is usually two lines down, past a "Type:" line
            look = look.strip()
            if look.startswith("Target:"):
                break
            if look.startswith("User:"):
                username = look.split("User:", 1)[1].strip()
                break
        pairs.append((service, username))
    return pairs


def _stored_targets():
    """The (service, username) pairs in the Credential Manager. Raises OSError / SubprocessError when cmdkey cannot run."""
    out = subprocess.run(["cmdkey", "/list"], capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace").stdout
    return parse_cmdkey(out)


def last_access_dates(path=None):
    """-> {(service, username): newest ISO timestamp} from the access trail. A missing file is {}; bad lines are skipped; only the last 5 MB is read."""
    p = Path(path) if path else Path(LOGS) / ACCESS_NAME
    last = {}
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > MAX_READ_BYTES:
                f.seek(size - MAX_READ_BYTES)
                f.readline()
            raw = f.read()
    except OSError:
        return last
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(line)
            key, ts = (e["service"], e["username"]), str(e["timestamp"])
        except (ValueError, KeyError, TypeError):
            continue
        if key not in last or ts > last[key]:
            last[key] = ts
    return last


def build_report(stored, last_access, scope, now=None, stale_days=STALE_DAYS):
    """Pure. -> {"timestamp", "credentials": [row], "stale": [row], "never_logged": [row]}; a row is {service, username, last_access, age_days}."""
    now = now or dt.datetime.now()
    rows = []
    for service, username in stored:
        if not in_scope(service, username, scope):
            continue
        ts = last_access.get((service, username))
        age = None
        if ts:
            try:
                age = max(0, (now - dt.datetime.fromisoformat(ts)).days)
            except ValueError:
                ts = None
        rows.append({"service": service, "username": username, "last_access": ts, "age_days": age})
    return {
        "timestamp": now.isoformat(timespec="seconds"),
        "credentials": rows,
        "stale": [r for r in rows if r["age_days"] is not None and r["age_days"] > stale_days],
        "never_logged": [r for r in rows if r["last_access"] is None],
    }


def check(now=None):
    """Read the store and the trail and build the report. Adds an "error" key when the store cannot be read."""
    scope = load_scope()
    try:
        stored = _stored_targets()
        error = None
    except (OSError, subprocess.SubprocessError) as e:
        stored, error = [], "cmdkey could not run (%s)" % type(e).__name__
    report = build_report(stored, last_access_dates(), scope, now)
    if error:
        report["error"] = error
    return report


def _names(rows):
    return ", ".join("%s/%s" % (r["service"], r["username"]) for r in rows)


def _alert(text):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source="keyring_audit", logs=str(LOGS))
    except Exception:
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform != "win32":
        print("keyring_audit: needs the Windows Credential Manager (cmdkey); nothing to do on this platform")
        return 0
    job = "--job" in argv
    if job and (Path(LOGS) / OFF_NAME).exists():
        print("keyring_audit: off")
        return 0
    r = check()
    if job:
        if r.get("error"):
            print("keyring_audit: " + r["error"])
        elif r["stale"]:
            _alert("%d credential(s) unused for %d+ days (rotate or remove): %s" % (len(r["stale"]), STALE_DAYS, _names(r["stale"])))
        print("keyring_audit: %d credential(s) in scope, %d stale" % (len(r["credentials"]), len(r["stale"])))
        return 0
    if r.get("error"):
        print("Could not read the Credential Manager: " + r["error"])
        return 0
    print("Assistant credentials in the Windows Credential Manager: %d" % len(r["credentials"]))
    for row in r["credentials"]:
        age = "%dd ago" % row["age_days"] if row["age_days"] is not None else "never logged via audited_keyring"
        print("  - %s / %s: last used %s" % (row["service"], row["username"], age))
    if r["stale"]:
        print("\n[STALE, %d+ days unused] %s" % (STALE_DAYS, _names(r["stale"])))
    if r["never_logged"]:
        print("[NEVER LOOKED UP VIA audited_keyring] " + _names(r["never_logged"]))
    if not r["credentials"]:
        print("\nNothing in scope. Name credentials with the prefix '%s' or list them in logs/%s." % (DEFAULT_PREFIX, SERVICES_FILE))
    elif not r["stale"] and not r["never_logged"]:
        print("\n[OK] nothing stale")
    return 0


if __name__ == "__main__":
    sys.exit(main())
