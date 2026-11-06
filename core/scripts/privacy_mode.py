# -*- coding: utf-8 -*-
"""Privacy mode: a switch that makes screen capture refuse.

While it is on, desktop_screenshot.py (and everything built on it, like desktop_control.see()) raises an error instead of taking a picture, and the optional
privacy_mode_hook.py blocks screenshot-type MCP tools before they run. The refusal is loud on purpose: a blocked capture must never look like an empty result.

    python privacy_mode.py on [minutes]   # no minutes = stays on until you turn it off; with minutes it switches itself off after that long
    python privacy_mode.py off
    python privacy_mode.py status

The switch is the file logs/privacy_mode.json ({"active": true, "until": <epoch seconds or null>}). It is written atomically.
A flag file that exists but cannot be read is treated as ON (a privacy switch must fail closed); `off` rewrites it and puts things right.

Library use:
    from privacy_mode import is_active, enable, disable
    if is_active():
        ...
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs). Works on Windows, macOS and Linux.
Pure core (unit tested): parse_minutes, read_flag, is_active, status_text."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

FLAG_NAME = "privacy_mode.json"
MAX_MINUTES = 60 * 24 * 365


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


def flag_file(logs=None):
    return Path(logs) / FLAG_NAME if logs else _logs() / FLAG_NAME


def _write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_minutes(text):
    """'30' -> 30. Raises ValueError for anything that is not a whole number of minutes from 1 to a year."""
    try:
        n = int(str(text).strip())
    except (TypeError, ValueError):
        raise ValueError("minutes must be a whole number, got %r" % (text,))
    if n < 1 or n > MAX_MINUTES:
        raise ValueError("minutes must be between 1 and %d" % MAX_MINUTES)
    return n


def read_flag(logs=None):
    """-> ('missing', None) | ('ok', dict) | ('bad', None). Never raises."""
    p = flag_file(logs)
    try:
        if not p.exists():
            return "missing", None
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "bad", None
    if not isinstance(data, dict):
        return "bad", None
    until = data.get("until")
    if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float))):
        return "bad", None
    return "ok", data


def enable(minutes=None, logs=None, now=None):
    """Switch privacy mode on (for `minutes` minutes, or until switched off). -> the data written."""
    now = time.time() if now is None else now
    data = {"active": True, "until": (now + parse_minutes(minutes) * 60) if minutes else None}
    _write_atomic(flag_file(logs), data)
    return data


def disable(logs=None):
    """Switch privacy mode off. Safe to run twice."""
    _write_atomic(flag_file(logs), {"active": False, "until": None})


def is_active(logs=None, now=None):
    """True while capture must be refused. An expired timer switches the mode off. A flag file that cannot be read counts as ON."""
    now = time.time() if now is None else now
    state, data = read_flag(logs)
    if state == "missing":
        return False
    if state == "bad":
        return True
    if not data.get("active"):
        return False
    until = data.get("until")
    if until is not None and now >= until:
        try:
            disable(logs)
        except OSError:
            pass
        return False
    return True


def status_text(logs=None, now=None):
    """'off' | 'on (indefinite)' | 'on (N min left)' | 'on (flag file unreadable, treated as on; run off to reset)'."""
    now = time.time() if now is None else now
    if not is_active(logs, now):
        return "off"
    state, data = read_flag(logs)
    if state == "bad":
        return "on (flag file unreadable, treated as on; run off to reset)"
    until = data.get("until")
    if until is None:
        return "on (indefinite)"
    return "on (%d min left)" % max(1, round(max(0, until - now) / 60))


status = status_text

USAGE = "usage: privacy_mode.py on [minutes] | off | status"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(USAGE)
        return 1
    cmd = argv[0].lower()
    try:
        if cmd == "on":
            minutes = parse_minutes(argv[1]) if len(argv) > 1 else None
            enable(minutes)
            print("Privacy mode ON%s - screenshots and screen capture are blocked." % (" for %d min" % minutes if minutes else " (until turned off)"))
            return 0
        if cmd == "off":
            disable()
            print("Privacy mode OFF.")
            return 0
        if cmd == "status":
            print(status_text())
            return 0
    except ValueError as e:
        print("privacy_mode: %s" % e)
        return 1
    except OSError as e:
        print("privacy_mode: could not write the switch file (%s)" % type(e).__name__)
        return 1
    print(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
