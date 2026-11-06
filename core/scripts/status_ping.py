# -*- coding: utf-8 -*-
"""Status ping: after a long silence, one short "still working" line, so the user is never left wondering.

Problem: the user sends a request, the assistant works for a long time without a reply, and the user cannot tell
whether it is working or stuck. This job notices that an assistant turn has been open a long time and says so, once
in a while, through notify.alert (a line in logs/alerts.txt, plus a desktop toast when the user switched toasts on).

It needs two facts, both kept as heartbeat files in logs/heartbeats/:
  user_prompt.beat    the time of the user's latest message. This script writes it: register it as a UserPromptSubmit hook
                      (it recognises the hook's JSON on stdin and writes the file silently), or run `status_ping.py --touch-user`.
  claude_session.beat the time the assistant last finished a turn (stop_gate.py writes it on every Stop).
A turn is "open" when the user's latest message is newer than the assistant's last finished turn.

A ping goes out when ALL hold: presence mode is home (logs/presence_mode.txt, see set_presence_mode.py; a missing file counts as home);
a turn has been open for more than 12 minutes (--silence-min) but less than 3 hours (--max-open-min: after that the session is
probably gone, and a "still working" line would be a guess); the last ping was more than 15 minutes ago (--gap-min); and the
kill switch logs/status_ping_off.txt is absent. The line quotes the CURRENT: line of logs/focus.md (or its first plain line), then says
how long it has been since the user's message: "still working: <focus> (no reply yet, 25 min since your last message)".
It states facts it knows; it does not claim progress.

    python status_ping.py [--dry-run]      # decide; --dry-run prints what it would send and sends nothing
    python status_ping.py --touch-user     # record "the user just wrote" (the hook does this automatically)
Optional: add logs/os_toast_on.txt to get a desktop toast as well as the alerts.txt line.
State: logs/status_ping_state.json. Environment: HARNESS_ROOT, HARNESS_LOGS.
Pure core (unit tested): decide, focus_line, make_text, presence_mode.
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

STATE_NAME = "status_ping_state.json"
OFF_NAME = "status_ping_off.txt"
USER_BEAT = "user_prompt"
SESSION_BEAT = "claude_session"
SILENCE_S = 12 * 60
GAP_S = 15 * 60
MAX_OPEN_S = 3 * 3600
FOCUS_CAP = 140
MAX_HOOK_BYTES = 1_000_000


def _logs(logs=None):
    return Path(logs) if logs else LOGS


def beat_time(name, logs=None, now=None):
    """The epoch a heartbeat was last written, or None when it is missing or unreadable."""
    import heartbeat
    now = time.time() if now is None else now
    age = heartbeat.age_seconds(name, _logs(logs) / "heartbeats", now)
    return None if age is None else now - age


def touch_user(logs=None, now=None):
    """Record that the user just wrote a message. Never raises; -> True when written."""
    try:
        import heartbeat
        heartbeat.touch(USER_BEAT, _logs(logs) / "heartbeats", now)
        return True
    except Exception:                                                     # noqa: BLE001
        return False


def presence_mode(logs=None):
    """'home' | 'away' | 'asleep'. A missing or unreadable file, or any other word, counts as home."""
    try:
        v = (_logs(logs) / "presence_mode.txt").read_text(encoding="utf-8").strip().lower()
    except (OSError, ValueError):
        return "home"
    return v if v in ("home", "away", "asleep") else "home"


def decide(now, prompt_at, stop_at, last_ping, mode="home", silence_s=SILENCE_S, gap_s=GAP_S, max_open_s=MAX_OPEN_S):
    """Pure. -> (send, reason). All times are epoch seconds (None = never). The boundary values do NOT pass (strict comparisons)."""
    if mode != "home":
        return False, "presence is %s" % mode
    if prompt_at is None:
        return False, "no user message recorded yet"
    if stop_at is not None and stop_at >= prompt_at:
        return False, "the last turn is finished, nothing is open"
    open_for = now - prompt_at
    if open_for <= silence_s:
        return False, "the turn has been open only %d min" % (open_for // 60)
    if open_for >= max_open_s:
        return False, "the turn has been open %d min, the session is probably gone" % (open_for // 60)
    if now - (last_ping or 0) <= gap_s:
        return False, "the last ping was %d min ago" % ((now - last_ping) // 60)
    return True, "turn open %d min" % (open_for // 60)


def focus_line(logs=None):
    """The CURRENT: line of logs/focus.md; else its first plain line (not blank, not a heading, not a RULE: line); else ''."""
    try:
        with (_logs(logs) / "focus.md").open("r", encoding="utf-8", errors="replace") as f:
            lines = f.read(20000).splitlines()
    except OSError:
        return ""
    first = ""
    for raw in lines:
        ln = raw.strip()
        if ln.startswith("CURRENT:"):
            return " ".join(ln[len("CURRENT:"):].split())[:FOCUS_CAP]
        if not first and ln and not ln.startswith(("#", "RULE:", "---")):
            first = " ".join(ln.split())[:FOCUS_CAP]
    return first


def make_text(focus, open_min):
    return "still working: %s (no reply yet, %d min since your last message)" % (focus or "no summary set in logs/focus.md", open_min)


def _load_state(logs):
    try:
        with (_logs(logs) / STATE_NAME).open("r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(logs, state):
    path = _logs(logs) / STATE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def run(logs=None, now=None, dry=False, silence_s=SILENCE_S, gap_s=GAP_S, max_open_s=MAX_OPEN_S, alert=None):
    """One decision. -> (sent, message). `alert` is injectable for tests; the default is notify.alert."""
    logs = _logs(logs)
    now = time.time() if now is None else now
    if (logs / OFF_NAME).exists():
        return False, "off"
    state = _load_state(logs)
    try:
        last_ping = float(state.get("t", 0) or 0)
    except (TypeError, ValueError):
        last_ping = 0.0
    prompt_at = beat_time(USER_BEAT, logs, now)
    send, why = decide(now, prompt_at, beat_time(SESSION_BEAT, logs, now), last_ping, presence_mode(logs), silence_s, gap_s, max_open_s)
    if not send:
        return False, "no ping: " + why
    text = make_text(focus_line(logs), int((now - prompt_at) // 60))
    if dry:
        return False, "WOULD SEND: " + text
    try:
        if alert is None:
            import notify
            alert = notify.alert
        alert(text, source="status_ping", toast=True, logs=str(logs))
    except Exception as e:                                                # noqa: BLE001
        return False, "could not send (%s)" % type(e).__name__
    _save_state(logs, {"t": now, "text": text})
    return True, "sent: " + text


def _hook_event():
    """The hook_event_name when stdin carries a hook's JSON, else None. Only reads stdin when it is a pipe or file (never a terminal)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        data = json.loads(sys.stdin.buffer.read(MAX_HOOK_BYTES).decode("utf-8-sig", errors="replace"))
        name = data.get("hook_event_name") if isinstance(data, dict) else None
        return name if isinstance(name, str) else None
    except Exception:                                                     # noqa: BLE001
        return None


def _minutes(argv, flag, default_s):
    if flag not in argv:
        return default_s
    try:
        return max(0.0, float(argv[argv.index(flag) + 1])) * 60
    except (IndexError, ValueError):
        return default_s


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if "--touch-user" in argv:
        touch_user()
        return 0
    if not argv:
        event = _hook_event()
        if event is not None:                                             # called as a hook: record the prompt, say nothing, never run the job
            if event == "UserPromptSubmit":
                touch_user()
            return 0
    sent, message = run(dry="--dry-run" in argv, silence_s=_minutes(argv, "--silence-min", SILENCE_S), gap_s=_minutes(argv, "--gap-min", GAP_S),
                        max_open_s=_minutes(argv, "--max-open-min", MAX_OPEN_S))
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
