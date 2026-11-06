# -*- coding: utf-8 -*-
"""UserPromptSubmit hook: puts the facts an assistant keeps getting wrong in front of it on every prompt.

Prints up to three parts, in this order:
  1. The real local date and time, one line:
       [Current date/time: Saturday, October 31, 2026, 01:22 PM local time.]
     (Any claim about the time of day made from memory drifts; a check on every prompt does not.)
  2. The standing focus file logs/focus.md as "[FOCUS]" plus its text, capped at 900 characters.
     Lines that start with "RULE:" are moved after the real focus lines, so when the cap cuts
     something it cuts standing rules and never the current task. If the file is missing:
       [FOCUS file missing: create logs/focus.md]
  3. The next item from the intent queue (intent_store.py), one line:
       [NEXT INTENT <id> tier<N> value<N>] <what>  (claim it, do it, then intent_store.py done <id>)
       [NEXT INTENT] none: <why there is nothing to do>
       [NEXT INTENT] paused: <reason>  (...)      when logs/loop_halt.txt or logs/intents_auto_off.txt exists

Kill switch: when logs/prompt_context_off.txt exists the hook prints nothing.
A hook must never break a prompt: every part is wrapped, errors print nothing, exit code is always 0.

Pure functions (unit tested): time_line, focus_line, halt_file, paused_line, intent_line, read_stdin.
Usage (hook command): python prompt_context_hook.py
"""
import datetime as dt
import json
import os
import sys
from pathlib import Path

# Root and paths
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

FOCUS_FILE = LOGS / "focus.md"
OFF_FILE = LOGS / "prompt_context_off.txt"
HALT_FILES = ("loop_halt.txt", "intents_auto_off.txt")
FOCUS_CAP = 900
WHAT_CAP = 150
REASON_CAP = 150
FOCUS_MISSING = "[FOCUS file missing: create logs/focus.md]"


def read_stdin() -> dict:
    """Drain and parse the hook JSON from stdin as UTF-8 bytes. Returns {} on any problem or when stdin is a terminal.
    The payload is not used for anything yet; reading it keeps the parent process from seeing a broken pipe."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.buffer.read()
        data = json.loads(raw.decode("utf-8-sig", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def time_line(now=None) -> str:
    """One line with the real local date and time. `now` is injectable for tests."""
    now = now or dt.datetime.now()
    return f"[Current date/time: {now.strftime('%A, %B %d, %Y, %I:%M %p')} local time.]"


def focus_line() -> str:
    """The standing focus file, capped; never raises.
    Real focus lines come first and "RULE:" lines after them, so the cap cuts the rules and not the current task."""
    try:
        txt = FOCUS_FILE.read_text(encoding="utf-8", errors="replace").strip()
        lines = [ln for ln in txt.splitlines() if ln.strip()]
        rules = [ln for ln in lines if ln.startswith("RULE:")]
        # Blank lines are dropped and rules go last, so the cap spends its budget on real focus lines first.
        txt = "\n".join([ln for ln in lines if not ln.startswith("RULE:")] + rules)
        return "[FOCUS]\n" + txt[:FOCUS_CAP]
    except Exception:
        return FOCUS_MISSING


def halt_file():
    """The first halt file that exists in the logs folder (a Path), or None."""
    for name in HALT_FILES:
        p = LOGS / name
        if p.exists():
            return p
    return None


def paused_line(halt) -> str:
    """The queue-paused line, with the first line of the halt file as the reason."""
    try:
        reason = halt.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0][:REASON_CAP]
    except (OSError, IndexError):
        reason = "no reason written"
    return f"[NEXT INTENT] paused: {reason}  (halted by logs/{halt.name}; delete that file to resume once the cause is understood)"


def _load_intent_store():
    """Import the kit's intent_store from the scripts folder, wherever the hook was started from."""
    p = str(SCRIPTS)
    if p not in sys.path:
        sys.path.insert(0, p)
    import intent_store
    return intent_store


def intent_line() -> str:
    """One line naming the top actionable item from the intent store, or why there is none.
    Returns "" on any problem, so the hook can never break a prompt."""
    try:
        halt = halt_file()
        if halt:
            return paused_line(halt)
        store = _load_intent_store()
        os.environ.setdefault("HARNESS_LOGS", str(LOGS))     # the store's demotion check reads this; keep it on the same folder as the queue
        intents = store.read_jsonl(store.INTENTS, str(LOGS))
        store.expire_leases(intents)
        top, why = store.next_action(intents)
        if top:
            done_when = store.describe_check(top.get("check"))
            return "[NEXT INTENT %s tier%d value%d] %s  (claim it, do it, then intent_store.py done <id>%s)" % (
                top["id"], int(top["tier"]), int(top["value"]), str(top["what"])[:WHAT_CAP],
                "; closes only when: " + done_when if done_when else "")
        return "[NEXT INTENT] none: " + why
    except Exception:
        return ""


def _utf8_stdout() -> None:
    """Hook output is read as UTF-8; a narrow console encoding must not turn an accent or dash into a crash."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


def main() -> None:
    """Entry point: print the three context parts unless the kill switch file exists. Never raises."""
    try:
        _utf8_stdout()
        read_stdin()
        if OFF_FILE.exists():
            return
        parts = [time_line(), focus_line(), intent_line()]
        sys.stdout.write("\n".join(p for p in parts if p) + "\n")
    except Exception:
        # Never raise and never print half an error into the prompt context
        pass


if __name__ == "__main__":
    main()
