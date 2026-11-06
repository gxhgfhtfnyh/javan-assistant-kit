# -*- coding: utf-8 -*-
"""A thin wrapper around the `keyring` package that writes an access trail: every credential lookup is logged (when, service, username, found yes/no),
never the secret itself.

    from audited_keyring import get_password        # instead of: from keyring import get_password
    token = get_password("my-service", "me")

Each lookup appends one line to logs/keyring_access.jsonl:
    {"timestamp": "2026-10-31T12:00:00", "service": "my-service", "username": "me", "found": true}
`keyring_audit.py` reads this trail to show which stored credentials have not been used for a long time.

Setup: `pip install keyring` (the OS keychain: Windows Credential Manager, macOS Keychain, Linux Secret Service). Without the package a lookup returns None,
logs an `error` field and prints one line to stderr; it never raises into the caller.
Kill switch: a file logs/keyring_access_off.txt stops the logging (lookups still work).
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).

CLI: python audited_keyring.py     prints the last 20 logged lookups.
Pure core (unit tested): clip, make_entry, log_access, read_entries."""
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

LOG_NAME = "keyring_access.jsonl"
OFF_NAME = "keyring_access_off.txt"
MAX_FIELD = 200                  # a service or user name longer than this is cut
MAX_LOG_BYTES = 2_000_000        # past this the log is cut back to its newest lines
KEEP_LINES = 5000
MAX_READ_BYTES = 1_000_000       # the CLI never reads more than the tail of the log

_warned = {"missing": False}


def clip(value):
    """A name made safe for the log: printable characters only, at most MAX_FIELD long. None stays None."""
    if value is None:
        return None
    return "".join(ch if ch.isprintable() else " " for ch in str(value))[:MAX_FIELD]


def make_entry(service, username, found, error=None, now=None):
    """The log line as a dict. It never carries the secret."""
    entry = {
        "timestamp": (now or dt.datetime.now()).isoformat(timespec="seconds"),
        "service": clip(service),
        "username": clip(username),
        "found": bool(found),
    }
    if error:
        entry["error"] = clip(error)
    return entry


def log_path():
    return Path(LOGS) / LOG_NAME


def _trim_if_big(path):
    """Cut a log that has grown past MAX_LOG_BYTES back to its newest KEEP_LINES lines (atomic replace)."""
    try:
        if path.stat().st_size <= MAX_LOG_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-KEEP_LINES:]
        tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def log_access(service, username, found, error=None, now=None):
    """Append one line to the access trail. -> True when written. Never raises."""
    try:
        if (Path(LOGS) / OFF_NAME).exists():
            return False
        entry = make_entry(service, username, found, error, now)
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _trim_if_big(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _load_keyring():
    """The keyring module, or None when it is not installed (or cannot be imported)."""
    try:
        import keyring
        return keyring
    except Exception:
        return None


def get_password(service, username):
    """Look a credential up through keyring, log the lookup, return the value (or None). Never raises."""
    kr = _load_keyring()
    value, error = None, None
    if kr is None:
        error = "keyring not installed"
        if not _warned["missing"]:
            _warned["missing"] = True
            print("audited_keyring: the keyring package is not installed (pip install keyring); lookups return None", file=sys.stderr)
    else:
        try:
            value = kr.get_password(service, username)
        except Exception as e:
            error = type(e).__name__
    log_access(service, username, value is not None, error)
    return value


def read_entries(path=None, max_bytes=MAX_READ_BYTES):
    """The well-formed log lines (oldest first), reading at most the last `max_bytes` of the file. A bad line is skipped; a missing file is []."""
    p = Path(path) if path else log_path()
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()                 # drop the partial first line
            raw = f.read()
    except OSError:
        return []
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and "service" in e and "timestamp" in e:
            out.append(e)
    return out


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    entries = read_entries()
    if not entries:
        print("No keyring access logged yet.")
        return 0
    for e in entries[-20:]:
        print("%s  %s/%s  found=%s" % (e.get("timestamp"), e.get("service"), e.get("username"), e.get("found")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
