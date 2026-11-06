# -*- coding: utf-8 -*-
"""Presence mode: is the user at home (at the keyboard), away, or asleep?

One tiny file, logs/presence_mode.txt, holds one word: home, away or asleep. Other scripts read it:
harness_health.py stays quiet at night while the mode is "asleep" (only the critical alarms ring), and
status_ping.py only sends its "still working" line while the mode is "home". Nothing here sends
anything anywhere: no chat message, no status push, no network. It writes the file and one history line.

    python set_presence_mode.py home|away|asleep [--by NAME]   # set the mode
    python set_presence_mode.py                                # print the current mode
    python set_presence_mode.py --history [N]                  # the last N changes (default 10)

A missing or unreadable file means "home". `--by` (or the environment variable PRESENCE_CHANGED_BY)
records who changed it, for example "user", "assistant" or "schedule"; the default is "manual".
Each change appends one JSON line to logs/presence_history.jsonl: {"ts", "iso", "mode", "prev", "by"}.
Setting the mode it already has changes nothing and adds no history line. The history file is trimmed to
its last 500 lines once it passes 200 KB.

Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): normalize, get_mode, set_mode, read_history.
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

VALID = ("home", "away", "asleep")
MODE_NAME = "presence_mode.txt"
HISTORY_NAME = "presence_history.jsonl"
HISTORY_MAX_BYTES = 200_000
HISTORY_KEEP = 500
BY_MAX = 40


def _logs(logs=None):
    return Path(logs) if logs else LOGS


def normalize(value):
    """-> 'home' | 'away' | 'asleep', or None when the text is not one of the three modes."""
    v = str(value or "").strip().lower()
    return v if v in VALID else None


def get_mode(logs=None):
    """The current mode. A missing, empty or unreadable file (or any other word in it) counts as 'home'."""
    try:
        text = (_logs(logs) / MODE_NAME).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return "home"
    return normalize(text) or "home"


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(5):                         # a reader holding the file open can make replace fail for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.05 * (attempt + 1))


def _clean_by(by):
    text = "".join(ch if ch.isprintable() else " " for ch in str(by or ""))
    return " ".join(text.split())[:BY_MAX] or "manual"


def _append_history(row, logs):
    path = _logs(logs) / HISTORY_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    try:
        if path.stat().st_size > HISTORY_MAX_BYTES:
            keep = path.read_text(encoding="utf-8", errors="replace").splitlines()[-HISTORY_KEEP:]
            _atomic_write(path, "\n".join(keep) + "\n")
    except OSError:
        pass


def set_mode(mode, by=None, logs=None, now=None):
    """Write the mode file and (when the mode really changed) one history line.
    -> {"mode", "prev", "changed"}. Raises ValueError for a word that is not home, away or asleep."""
    new = normalize(mode)
    if new is None:
        raise ValueError("mode must be one of %s (got %r)" % ("|".join(VALID), str(mode)[:30]))
    prev = get_mode(logs)
    path = _logs(logs) / MODE_NAME
    changed = new != prev
    if changed or not path.exists():
        _atomic_write(path, new)
    if changed:
        ts = time.time() if now is None else now
        row = {"ts": ts, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)), "mode": new, "prev": prev,
               "by": _clean_by(by or os.environ.get("PRESENCE_CHANGED_BY"))}
        try:
            _append_history(row, logs)
        except OSError:
            pass                                      # the mode itself is set; a history failure must not undo it
    return {"mode": new, "prev": prev, "changed": changed}


def read_history(n=10, logs=None):
    """The last n history rows, oldest first. Lines that are not JSON objects are skipped."""
    try:
        text = (_logs(logs) / HISTORY_NAME).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    k = max(0, int(n))
    return rows[-k:] if k else []


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if "--history" in argv:
        i = argv.index("--history")
        try:
            n = int(argv[i + 1]) if i + 1 < len(argv) else 10
        except ValueError:
            n = 10
        rows = read_history(n)
        for r in rows:
            print("%s %s (was %s, by %s)" % (r.get("iso", "?"), r.get("mode", "?"), r.get("prev", "?"), r.get("by", "?")))
        if not rows:
            print("no changes recorded")
        return 0
    by = None
    if "--by" in argv:
        i = argv.index("--by")
        by = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
    if not argv:
        print(get_mode())
        return 0
    try:
        r = set_mode(argv[0], by=by)
    except ValueError as e:
        print("Usage: python set_presence_mode.py home|away|asleep [--by NAME]  (%s)" % e)
        return 1
    except OSError as e:
        print("could not write the mode file: %s" % type(e).__name__)
        return 1
    print("presence mode set: %s" % r["mode"] if r["changed"] else "presence mode already %s" % r["mode"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
