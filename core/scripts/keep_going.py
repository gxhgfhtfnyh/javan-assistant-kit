# -*- coding: utf-8 -*-
"""Keep-going Stop hook for Claude Code.

Problem it solves: the assistant closes a turn with "next I'm doing X" and the turn simply ends,
so X never starts until the user pings again.

This hook reads the last assistant text from the transcript. If that text promises a next step the
assistant can do itself and does not say it is waiting on the user, the hook BLOCKS the stop once
with a reason telling the assistant to do that step now with a tool call.

It also holds a stop open while the standing work queue (logs/queue.md) still has open items:
  - [ ] item                      open: work it
  - [x] item                      done
  - [~] item (waiting: reason)    blocked on something outside the assistant; does not hold the stop,
                                  EXCEPT a waiting item that talks about background work (background,
                                  running, batch, benchmark, job) without naming a task id that will
                                  wake the assistant: nothing would ever wake it, so it counts as open.

Loop guards: never blocks when stop_hook_active is true (already continuing because of a hook), and
blocks at most MAX_BLOCKS_PER_HOUR times per hour (state in logs/keep_going_state.json).
Kill switch: create logs/keep_going_off.txt. Every decision is appended to logs/keep_going.jsonl.
Held-open intents are handled by stop_gate.py, not here.

Reads hook JSON from stdin (fields: transcript_path, stop_hook_active). Prints nothing to allow, or
{"decision": "block", "reason": "..."} to block. Never raises.

Pure functions: should_block(), build_message(), read_queue_open(), notifier_pending().
Run: python keep_going.py
"""
import json
import os
import re
import sys
import time
from pathlib import Path

# Root and paths
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

LOG = LOGS / "keep_going.jsonl"
STATE = LOGS / "keep_going_state.json"
OFF = LOGS / "keep_going_off.txt"
QUEUE = LOGS / "queue.md"

MAX_BLOCKS_PER_HOUR = 6
TAIL_CHARS = 500                    # only the end of the last message counts as "how the turn closed"
TRANSCRIPT_TAIL_LINES = 300
TRANSCRIPT_TAIL_BYTES = 4000000     # never read a whole multi-hundred-MB transcript

PROMISE = re.compile(r"(\bnext(?: step)?(?: is| up| i'?m| i will| i'?ll)\b|\bi'?ll now\b|\bnow i'?(?:m|ll)\b|(?<!how )\bi'?m (?:now )?(?:going to|about to|starting|building|doing|checking|looking|working on)\b|\bgoing to (?:build|do|check|start|write|fix|look)\b|\bnext i'?m\b)", re.I)
WAITING = re.compile(r"(wait(?:ing)? (?:on|for)|until you|need you to|your call|tell me (?:if|which|what)|once you (?:send|say|reply|answer)|when you (?:send|say|reply)|if you want|say the word|nothing (?:else )?(?:left|pending)|all done|every item is done|only stop when)", re.I)

OPEN_ITEM = re.compile(r"^\s*-\s*\[ \]\s*(.+?)\s*$")
WAIT_ITEM = re.compile(r"^\s*-\s*\[~\]\s*(.+?)\s*$")
BACKGROUND_WORDS = re.compile(r"background|running|batch|benchmark|job", re.I)
# A real task id has a digit in it; plain words like "background" and "benchmark" must not match.
TASK_ID = re.compile(r"\b(?=[a-z0-9]*\d)[ab][a-z0-9]{8,17}\b")


# --- queue ------------------------------------------------------------------------------------

def _read_lines(path) -> list:
    """Lines of a text file, or [] when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def read_queue_open(path=None) -> list:
    """Open items of the standing work queue.

    '- [ ] text' is open. '- [x]' (done) and '- [~] text (waiting: reason)' (blocked) do not hold a
    stop, except a waiting item that mentions background work but names no task id in its waiting
    reason: nothing would wake the assistant, so it counts as open.
    """
    out = []
    for line in _read_lines(QUEUE if path is None else path):
        m = OPEN_ITEM.match(line)
        if m:
            out.append(m.group(1))
            continue
        w = WAIT_ITEM.match(line)
        if w:
            item = w.group(1)
            if BACKGROUND_WORDS.search(item) and "waiting:" in item and not TASK_ID.search(item.split("waiting:", 1)[1]):
                out.append("[no notifying task named] " + item)
    return out


def notifier_pending(path=None) -> bool:
    """True when the queue has a '- [~]' item whose waiting reason names a real task id: a background job will wake the assistant."""
    for line in _read_lines(QUEUE if path is None else path):
        w = WAIT_ITEM.match(line)
        if w and "waiting:" in w.group(1) and TASK_ID.search(w.group(1).split("waiting:", 1)[1]):
            return True
    return False


# --- decision ---------------------------------------------------------------------------------

def normalise(text: str) -> str:
    """Curly apostrophes must match the same patterns as straight ones."""
    return text.replace("’", "'").replace("‘", "'")


def should_block(text, stop_hook_active=False, queue_open=None, waiting_on_job=False):
    """Return (block, reason).

    1. A forward promise in the last TAIL_CHARS characters with no waiting-on-the-user marker blocks.
    2. Any stop while queue_open is non-empty blocks (each item must be worked, or marked waiting / done).
    stop_hook_active always allows (loop guard). waiting_on_job is accepted for callers that know a
    notifying background job exists; it never causes or prevents a block, it only changes the logged
    reason when nothing is blocked.
    """
    if stop_hook_active:
        return False, "already continuing"
    if not (text or "").strip():
        return False, "no text"
    tail = normalise(text)[-TAIL_CHARS:]
    m = PROMISE.search(tail)
    if m and not WAITING.search(tail):
        return True, "promised '%s' and stopped" % m.group(0).strip()
    if queue_open:
        return True, "queue still has %d open item(s): %s" % (len(queue_open), "; ".join(q[:70] for q in queue_open[:2]))
    if not m:
        return False, "waiting on a background job" if waiting_on_job else "no promise"
    return False, "waiting on the user"


def build_message(why: str) -> str:
    """The text of the block reason shown to the assistant."""
    if why.startswith("queue"):
        return ("Your standing work queue still has doable items (" + why + "). Work the next one now with a tool call. "
                "If it truly cannot proceed, mark it '- [~] <item> (waiting: <reason>)' in " + str(QUEUE) + "; "
                "if it waits on a background job, put that job's task id in the reason so its notification wakes you. "
                "When it is finished mark it '- [x]'. Do not stop while open items remain.")
    return ("You ended your reply by promising a next step (" + why + "). Do that step now with a tool call instead of stopping. "
            "If it truly depends on the user, say what you are waiting on.")


# --- transcript -------------------------------------------------------------------------------

def tail_lines(path, max_lines=TRANSCRIPT_TAIL_LINES, max_bytes=TRANSCRIPT_TAIL_BYTES) -> list:
    """Last max_lines lines of the transcript as text, reading at most max_bytes from the end. [] when unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            start = max(0, f.tell() - max_bytes)
            f.seek(start)
            data = f.read()
    except (OSError, TypeError, ValueError):
        return []
    raw = data.split(b"\n")
    if start > 0 and raw:
        raw = raw[1:]          # the first line is probably cut in half
    kept = [b for b in raw if b.strip()]       # drop blank pieces first: the trailing newline would otherwise use up a slot
    return [b.decode("utf-8", errors="replace").rstrip("\r") for b in kept[-max_lines:]]


def parse_rows(lines) -> list:
    """JSON-decode each line; lines that are not valid JSON are skipped."""
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def last_assistant_text(rows) -> str:
    """Text of the last assistant message that has any (text blocks only; tool calls are ignored)."""
    last = ""
    for r in rows:
        if not isinstance(r, dict) or r.get("type") != "assistant":
            continue
        message = r.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            if content.strip():
                last = content
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                    last = b["text"]
    return last


# --- state, log, io ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with STATE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        with STATE.open("w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def blocks_this_hour(state: dict, now: float) -> list:
    """Timestamps of blocks within the last hour (anything that is not a number is ignored)."""
    blocks = state.get("blocks", []) if isinstance(state, dict) else []
    if not isinstance(blocks, list):
        return []
    return [t for t in blocks if isinstance(t, (int, float)) and not isinstance(t, bool) and now - t < 3600]


def record_block(state: dict, now: float) -> dict:
    """State with this block added (and blocks older than an hour dropped)."""
    blocks = blocks_this_hour(state, now)
    blocks.append(now)
    return {"blocks": blocks}


def log_decision(block: bool, why: str, text: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%F %T"), "block": block, "why": why, "tail": (text or "")[-160:].replace("\n", " ")}
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def is_off() -> bool:
    try:
        return OFF.exists()
    except Exception:
        return False


def read_stdin() -> dict:
    """Hook JSON from stdin, read as UTF-8 bytes. {} on any problem."""
    try:
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        raw = raw.lstrip("﻿")
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def main() -> None:
    """Entry point: read stdin, look at the last assistant message and the queue, maybe block."""
    try:
        if is_off():
            return
        data = read_stdin()
        path = data.get("transcript_path")
        if not path:
            return
        text = last_assistant_text(parse_rows(tail_lines(path)))
        state = load_state()
        now = time.time()
        recent = blocks_this_hour(state, now)
        block, why = should_block(text, bool(data.get("stop_hook_active")), read_queue_open(), notifier_pending())
        if block and len(recent) >= MAX_BLOCKS_PER_HOUR:
            block, why = False, "hourly cap reached"
        log_decision(block, why, text)
        if block:
            save_state(record_block(state, now))
            # ASCII-escaped on purpose: a Windows pipe may not be UTF-8 and a failed write would silently allow the stop
            sys.stdout.write(json.dumps({"decision": "block", "reason": build_message(why)}))
            sys.stdout.flush()
        # Not blocking: print nothing (empty stdout = allow)
    except Exception:
        # Never raise - just allow
        return


if __name__ == "__main__":
    main()
