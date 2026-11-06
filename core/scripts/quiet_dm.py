# -*- coding: utf-8 -*-
"""Quiet hours for unprompted, non-urgent messages: held while the owner is asleep, delivered as ONE message afterwards, never lost.

Part of the OPTIONAL chat channel. A non-urgent automated sender (a digest, a reminder, a sync note) calls quiet_dm.send(text, source="...") instead of messaging the owner itself:
    import quiet_dm
    quiet_dm.send("new announcement: week 12 is up", source="school")      # True when it was sent OR held; either way the caller can treat it as delivered
While the presence mode is `asleep` the text is held in logs/quiet_dm_queue.jsonl. A timed job runs `python scripts/quiet_dm.py`: once the presence mode is not asleep it sends everything held as
ONE message (split only if it would pass Discord's limit; identical items are merged into one line with a count). Safety and machinery alerts do not use this; they go straight through notify.alert.
Replies to the owner never touch it.
    python scripts/quiet_dm.py             # deliver what is held, if the owner is not asleep
    python scripts/quiet_dm.py status      # how many items are held, how old, and the presence mode
    python scripts/quiet_dm.py --dry-run   # show the message(s) that would be sent; send and remove nothing

PRESENCE: logs/presence_mode.txt holds one word: home, away or asleep. A missing, empty or unknown value counts as home. Whatever tells your assistant the owner is asleep (a bedtime phrase, a script) writes it.
DELIVERY: how a message is sent is a setting. In order: (1) logs/quiet_dm_sender.json {"argv": ["python", "scripts/some_sender.py", "--stdin"]}, run with the text on stdin and no shell;
(2) when the Discord pack is set up (logs/discord_config.json exists) python scripts/discord_queue_send.py --stdin --lint-ok; (3) otherwise a line in logs/alerts.txt through notify.alert.
Safety nets: a held item older than 12 hours is delivered even if presence still says asleep (a stuck mode must not swallow mail). logs/quiet_dm_off.txt turns holding off (messages go at once);
QUIET_DM_NOW=1 in the environment does the same for one run. Fail-open: any error while deciding sends the message immediately.
Sending is an action, so the timed job that runs this is shipped disabled; enable it when you have chosen a delivery route.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): build_messages(rows), holding(mode, off, env). Everything else takes its paths and clock as arguments."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

ROOT = Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

STALE_S = 12 * 3600
CHUNK = 1800                        # Discord rejects 2000+ characters; leave room for the header
HEADER = "came in while you were asleep:"
QUEUE_NAME = "quiet_dm_queue.jsonl"
OFF_NAME = "quiet_dm_off.txt"
SENDER_NAME = "quiet_dm_sender.json"
PRESENCE_NAME = "presence_mode.txt"
MODES = ("home", "away", "asleep")
MAX_ROWS = 2000                     # a queue longer than this is cut to the newest rows (a runaway sender must not fill the disk)
MAX_ITEM = 4000


def _logs(logs=None):
    return Path(logs) if logs else Path(LOGS)


def _q(logs=None):
    return _logs(logs) / QUEUE_NAME


def current_mode(logs=None):
    """The presence mode from logs/presence_mode.txt; home when the file is missing, unreadable or says something else."""
    try:
        value = (_logs(logs) / PRESENCE_NAME).read_text(encoding="utf-8-sig").strip().lower()      # a file written by Windows PowerShell starts with a BOM
        return value if value in MODES else "home"
    except OSError:
        return "home"


def holding(mode, off=False, env=None):
    """Pure. True when a non-urgent message should be held right now."""
    env = os.environ if env is None else env
    return mode == "asleep" and not off and not env.get("QUIET_DM_NOW")


def _off(logs=None):
    return (_logs(logs) / OFF_NAME).exists()


def sender_argv(logs=None):
    """-> the argv list that delivers one message (text on stdin), or None to use notify.alert. From logs/quiet_dm_sender.json, else the Discord pack when it is set up."""
    lg = _logs(logs)
    try:
        data = json.loads((lg / SENDER_NAME).read_text(encoding="utf-8-sig"))
        argv = data.get("argv") if isinstance(data, dict) else None
        if isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv):
            return [sys.executable if argv[0] == "python" else argv[0]] + argv[1:]
    except (OSError, ValueError):
        pass
    if (lg / "discord_config.json").exists():
        return [sys.executable, str(HERE / "discord_queue_send.py"), "--stdin", "--lint-ok"]
    return None


def _direct(text, logs=None):
    """Deliver one message the ordinary way. True when it was delivered."""
    argv = sender_argv(logs)
    if argv is None:
        try:
            import notify
            notify.alert(text, source="quiet_dm", logs=_logs(logs))
            return True
        except Exception:
            return False
    try:
        r = subprocess.run(argv, input=text, text=True, capture_output=True, encoding="utf-8", timeout=60, cwd=str(ROOT),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        return r.returncode == 0
    except Exception:
        return False


def _read(logs=None):
    rows = []
    try:
        with open(_q(logs), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if isinstance(r, dict) and isinstance(r.get("text"), str) and r.get("text"):
                        rows.append(r)
                except ValueError:
                    continue
    except OSError:
        pass
    return rows[-MAX_ROWS:]


def _write(rows, logs=None):
    path = _q(logs)
    if not rows:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _lock(logs=None):
    import loop_lock
    return loop_lock.locked("quiet_dm", "quiet_dm-%d" % os.getpid(), ttl=120, wait=30, logs=str(_logs(logs)))


def send(text, source="", logs=None, mode_now=None, sender=None, now=None):
    """Drop-in for a non-urgent unprompted message: True when it was sent OR held (callers treat both as delivered). Fail-open."""
    deliver = sender or (lambda t: _direct(t, logs))
    try:
        m = mode_now if mode_now is not None else current_mode(logs)
        if holding(m, _off(logs)):
            with _lock(logs):
                rows = _read(logs)
                rows.append({"ts": now if now is not None else time.time(), "source": str(source)[:60], "text": str(text)[:MAX_ITEM]})
                _write(rows, logs)
            return True
    except Exception:
        pass                                                   # could not hold it: fail open and deliver now
    return deliver(text)                                       # not holding, or holding failed: deliver exactly once


def build_messages(rows):
    """Pure. -> [(message text, [rows in it])]. Identical texts are merged into one line (with a count); items are kept whole, in the order they arrived;
    a message never exceeds CHUNK characters (an item bigger than that is cut)."""
    merged = []
    seen = {}
    for r in rows:
        t = (r.get("text") or "").strip()
        if not t:
            continue
        if t in seen:
            seen[t][1].append(r)
            continue
        seen[t] = [t, [r]]
        merged.append(seen[t])
    out, cur, cur_rows, size = [], [], [], len(HEADER)
    for t, rs in merged:
        body = t if len(rs) == 1 else "%s (x%d)" % (t, len(rs))
        if len(body) > CHUNK - 40:
            body = body[:CHUNK - 60].rstrip() + "\n(cut)"
        if cur and size + len(body) + 2 > CHUNK:
            out.append((HEADER + "\n\n" + "\n\n".join(cur), cur_rows))
            cur, cur_rows, size = [], [], len(HEADER)
        cur.append(body)
        cur_rows = cur_rows + rs
        size += len(body) + 2
    if cur:
        out.append((HEADER + "\n\n" + "\n\n".join(cur), cur_rows))
    return out


def flush(logs=None, mode_now=None, sender=None, now=None, dry_run=False):
    """Send what is held as one message once presence is not asleep (or the oldest item is stale). -> (items delivered, messages sent).
    With dry_run nothing is sent or removed; the result counts what WOULD be sent."""
    now = time.time() if now is None else now
    rows = _read(logs)
    if not rows:
        return 0, 0
    m = mode_now if mode_now is not None else current_mode(logs)
    oldest = min(float(r.get("ts", now)) for r in rows)
    if holding(m, _off(logs)) and now - oldest < STALE_S:
        return 0, 0
    deliver = sender or (lambda t: _direct(t, logs))
    if dry_run:
        parts = build_messages(rows)
        for text, _rs in parts:
            print("would send:\n" + text)
        return sum(len(rs) for _t, rs in parts), len(parts)
    delivered = msgs = 0
    with _lock(logs):
        rows = _read(logs)                                     # re-read under the lock: a sender may have added more
        parts = build_messages(rows)
        sent = set()
        for text, rs in parts:
            if not deliver(text):
                break                                          # keep this chunk and everything after it for the next run
            msgs += 1
            delivered += len(rs)
            sent.update((r.get("ts"), r.get("text")) for r in rs)
        _write([r for r in _read(logs) if (r.get("ts"), r.get("text")) not in sent], logs)       # re-read: whatever was appended meanwhile stays
    return delivered, msgs


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "status":
        rows = _read()
        age = (time.time() - min(float(r.get("ts", 0)) for r in rows)) / 60 if rows else 0
        print("held: %d item(s), oldest %.0f min; presence %s" % (len(rows), age, current_mode()))
        return 0
    dry = "--dry-run" in argv
    try:
        n, k = flush(dry_run=dry)
    except Exception as e:
        print("quiet_dm: could not deliver (%s); the held items are kept" % type(e).__name__)
        return 0
    if dry:
        print("quiet_dm: dry run, would deliver %d item(s) in %d message(s)" % (n, k) if n else "quiet_dm: dry run, nothing to deliver")
    else:
        print("quiet_dm: delivered %d item(s) in %d message(s)" % (n, k) if n else "quiet_dm: nothing to deliver")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
