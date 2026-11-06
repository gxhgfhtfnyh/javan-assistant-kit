# -*- coding: utf-8 -*-
"""Clipboard scrubber (Windows only): clears the clipboard a short while after something that looks like a secret was copied.

A pasted API key or token otherwise sits on the clipboard for hours, readable by every program that polls it. This watches the clipboard text every 2 seconds;
when it holds a likely secret for 45 seconds in a row it clears it. "Likely secret" means: a known key format (sk-..., ghp_..., xox..., AIza..., AKIA...), a private key
block, or a single 24-64 character token of letters and digits with enough randomness (hex strings need more randomness; a git hash can still be mistaken for one).
The text itself is never written anywhere: the log holds only the time, the kind and the length.

Safety design: it only polls the clipboard CONTENT with the ordinary open / read / close calls (inside try/finally); no keyboard hook, no low-level memory calls,
nothing that can disturb copy and paste in other programs. If it ever needs changing, keep it that way.

It ACTS on your clipboard, so it starts in report-only mode: it prints and logs "would clear" and touches nothing. To really clear:
    python clipboard_scrubber.py --clear        # this run clears
    or create the file logs/clipboard_scrubber_on.txt   # clears from then on, also for a run that is already going
Stop it with Ctrl+C or by creating logs/clipboard_scrubber_off.txt (checked every poll). One copy runs at a time (logs/clipboard_scrubber.pid, refreshed every poll).
When it clears, it adds a line to logs/alerts.txt (and a desktop notification if you turned those on, see notify.py). Log: logs/clipboard_scrubber.jsonl.
Needs the optional package pywin32:  pip install pywin32 . On macOS and Linux it prints one line and exits 0. Run it at logon with a scheduled task if you want it always on
(python or pythonw with the full path to this file); it is not in the job table because it is a long-running watcher, not a timed job.
Environment: HARNESS_ROOT, HARNESS_LOGS. Pure core (unit tested): classify, step, another_running."""
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).parent.resolve()
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

SOURCE = "clipboard_scrubber"
POLL_SECONDS = 2
GRACE_SECONDS = 45
RETRY_SECONDS = 5
MAX_TRIES = 3
LOCK_FRESH_SECONDS = 30
MAX_CLIP_CHARS = 100000
OFF_NAME = "clipboard_scrubber_off.txt"
ON_NAME = "clipboard_scrubber_on.txt"
PID_NAME = "clipboard_scrubber.pid"
LOG_NAME = "clipboard_scrubber.jsonl"

KNOWN_PREFIXES = re.compile(
    r"(?<![\w-])(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{30,}|(?:AKIA|ASIA)[A-Z0-9]{16})(?![\w-])"
)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")
GENERIC_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{24,64}$")


def logs_dir():
    value = os.environ.get("HARNESS_LOGS")
    if value:
        return Path(value).resolve()
    if os.environ.get("HARNESS_ROOT"):
        return Path(os.environ["HARNESS_ROOT"]).resolve() / "logs"
    return LOGS


def _entropy(s):
    if not s:
        return 0
    counts = {}
    for c in s:
        counts[c] = counts.get(c, 0) + 1
    return -sum((n / len(s)) * math.log2(n / len(s)) for n in counts.values())


def classify(text):
    """-> 'private_key' | 'known_key' | 'hex_token' | 'token' | None for a piece of clipboard text."""
    text = (text or "")[:MAX_CLIP_CHARS].strip()
    if not text:
        return None
    if PRIVATE_KEY_RE.search(text):
        return "private_key"
    if KNOWN_PREFIXES.search(text):
        return "known_key"
    if GENERIC_TOKEN_RE.match(text):
        if re.fullmatch(r"[0-9a-f]+", text, re.IGNORECASE):
            return "hex_token" if _entropy(text) >= 3.4 else None
        return "token" if _entropy(text) >= 3.0 and sum(c.isdigit() for c in text) >= 3 else None
    return None


def _looks_like_secret(text):
    return classify(text) is not None


def _digest(text):
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def fresh_state():
    return {"digest": None, "since": 0.0, "done": False, "tries": 0}


def step(state, current, now, grace=GRACE_SECONDS):
    """One poll of the state machine. -> (new state, action) with action None or 'clear'. The clipboard text is kept only as a hash.
    A secret must stay unchanged for `grace` seconds before 'clear'; different content or a non-secret restarts or ends the watch; after 'clear' nothing more happens until the content changes."""
    if not current or classify(current) is None:
        return fresh_state(), None
    d = _digest(current)
    if state.get("digest") != d:
        return {"digest": d, "since": now, "done": False, "tries": 0}, None
    if state.get("done"):
        return state, None
    if now - state["since"] >= grace:
        return dict(state, done=True, tries=state.get("tries", 0) + 1), "clear"
    return state, None


def retry_state(state, now, grace=GRACE_SECONDS):
    """After a clear that failed (clipboard busy): try again in RETRY_SECONDS, up to MAX_TRIES times, then give up on this content."""
    if state.get("tries", 0) >= MAX_TRIES:
        return state
    return dict(state, done=False, since=now - grace + RETRY_SECONDS)


# ---- the Windows clipboard (pywin32 is imported only here, so this file imports on every system)
def _win32clipboard():
    try:
        import win32clipboard
        return win32clipboard
    except ImportError:
        return None


def get_clipboard_text(api=None):
    """The clipboard's unicode text, or None (no text, or the clipboard is busy in another program this moment: skip, never block)."""
    api = api or _win32clipboard()
    if api is None:
        return None
    try:
        api.OpenClipboard()
        try:
            if api.IsClipboardFormatAvailable(api.CF_UNICODETEXT):
                return api.GetClipboardData(api.CF_UNICODETEXT)
            return None
        finally:
            api.CloseClipboard()
    except Exception:
        return None


def clear_clipboard(api=None):
    api = api or _win32clipboard()
    if api is None:
        return False
    try:
        api.OpenClipboard()
        try:
            api.EmptyClipboard()
        finally:
            api.CloseClipboard()
        return True
    except Exception:
        return False


# ---- single instance, log, alert
def another_running(now=None, me=None):
    """True when a pid file written by another process was refreshed in the last LOCK_FRESH_SECONDS (the running copy refreshes it every poll)."""
    p = logs_dir() / PID_NAME
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
        age = (time.time() if now is None else now) - p.stat().st_mtime
    except (OSError, ValueError):
        return False
    return pid != (os.getpid() if me is None else me) and age < LOCK_FRESH_SECONDS


def touch_pidfile():
    try:
        lg = logs_dir()
        lg.mkdir(parents=True, exist_ok=True)
        (lg / PID_NAME).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def remove_pidfile():
    p = logs_dir() / PID_NAME
    try:
        if p.read_text(encoding="utf-8").strip() == str(os.getpid()):
            p.unlink()
    except (OSError, ValueError):
        pass


def _log(event, **fields):
    """One row in logs/clipboard_scrubber.jsonl. Never holds clipboard text."""
    try:
        lg = logs_dir()
        lg.mkdir(parents=True, exist_ok=True)
        with (lg / LOG_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}) + "\n")
    except OSError:
        pass


def _alert(text):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source=SOURCE, toast=True, logs=str(logs_dir()))
        return
    except Exception:
        pass
    try:
        lg = logs_dir()
        lg.mkdir(parents=True, exist_ok=True)
        with (lg / "alerts.txt").open("a", encoding="utf-8") as f:
            f.write("[%s] %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), SOURCE, text))
    except OSError:
        pass


def clearing_enabled(flag=False):
    return bool(flag) or (logs_dir() / ON_NAME).exists()


# ---- the loop
def run(get_text=None, clear=None, flag=False, sleep=time.sleep, clock=time.time, max_ticks=None, say=print):
    """Poll until the off file appears (or `max_ticks` polls, for tests). `get_text` and `clear` are the clipboard functions, replaceable for tests. -> number of clears done."""
    get_text = get_text or get_clipboard_text
    clear = clear or clear_clipboard
    state, cleared, ticks = fresh_state(), 0, 0
    while max_ticks is None or ticks < max_ticks:
        ticks += 1
        if (logs_dir() / OFF_NAME).exists():
            say("clipboard_scrubber: off (logs/%s exists), stopping" % OFF_NAME)
            break
        touch_pidfile()
        text = get_text()
        now = clock()
        state, action = step(state, text, now)
        if action == "clear":
            kind, size = classify(text), len((text or "").strip())
            if not clearing_enabled(flag):
                _log("would_clear", kind=kind, chars=size)
                say("clipboard_scrubber: would clear a likely secret (%s, %d characters); report-only mode, run with --clear to really clear" % (kind, size))
            elif clear():
                cleared += 1
                _log("cleared", kind=kind, chars=size)
                _alert("clipboard auto-cleared (it looked like a secret: %s, %d characters)" % (kind, size))
            else:
                _log("clear_failed", kind=kind, chars=size, tries=state["tries"])
                state = retry_state(state, now)
        sleep(POLL_SECONDS)
    return cleared


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "-h" in argv or "--help" in argv:
        print(__doc__.split("\n\n")[0])
        print("Usage: python clipboard_scrubber.py [--clear]")
        return 0
    unknown = [a for a in argv if a != "--clear"]
    if unknown:
        print("Usage: python clipboard_scrubber.py [--clear]")
        return 2
    if sys.platform != "win32":
        print("clipboard_scrubber: Windows only (it uses the Windows clipboard API); nothing to do on this system.")
        return 0
    if (logs_dir() / OFF_NAME).exists():
        print("clipboard_scrubber: off (logs/%s exists)" % OFF_NAME)
        return 0
    if _win32clipboard() is None:
        print("clipboard_scrubber: needs the package pywin32 (pip install pywin32)")
        return 2
    if another_running():
        print("already running, exiting")
        return 0
    flag = "--clear" in argv
    mode = "will clear" if clearing_enabled(flag) else "report-only (use --clear or create logs/%s to really clear)" % ON_NAME
    print("Clipboard scrubber running (poll=%ds, grace=%ds), %s. Ctrl+C to stop." % (POLL_SECONDS, GRACE_SECONDS, mode))
    _log("started", mode="clear" if clearing_enabled(flag) else "report")
    try:
        run(flag=flag)
    except KeyboardInterrupt:
        print("stopped")
    finally:
        _log("stopped")
        remove_pidfile()
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
