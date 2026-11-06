# -*- coding: utf-8 -*-
"""PostToolUse hook: every tool call is proof that the live session is working, so it touches the 'claude_session' heartbeat.

Why: the stop hook and the session-start hook touch that heartbeat only at the start and the end of a turn. During one long turn (a big refactor,
an hour of tests) nothing would touch it, the health check would call the session dead, and the autopilot could start a second session beside
the live one. Touching it after every tool call keeps the heartbeat as fresh as the work is.

What it does: reads the hook JSON from stdin, and unless the call came from a sub-agent it writes logs/heartbeats/claude_session.beat with the kit's
heartbeat.py. Sub-agents are skipped (Claude Code adds agent_id / agent_type to their hook input) because a helper agent running does not mean the
main session is alive. It prints nothing, never blocks a tool, never raises, and always exits 0. It does nothing with chat messages.
Kill switch: logs/tool_heartbeat_off.txt (the hook does nothing).

Hook config: PostToolUse, no matcher (every tool).
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): is_subagent_call(hook), beat(logs, now), handle(payload, logs)."""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OFF_NAME = "tool_heartbeat_off.txt"
BEAT_NAME = "claude_session"
BEATS_DIR = "heartbeats"


def root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent)


def logs_dir():
    return Path(os.environ.get("HARNESS_LOGS") or root() / "logs")


def is_subagent_call(hook):
    """True when the hook input comes from a sub-agent's tool call (Claude Code adds agent_id / agent_type in that case)."""
    return isinstance(hook, dict) and bool(hook.get("agent_id") or hook.get("agent_type"))


def beat(logs, now=None):
    """Write the claude_session beat file with the kit's heartbeat.py. True when it was written; never raises."""
    try:
        p = str(HERE)
        if p not in sys.path:
            sys.path.insert(0, p)
        import heartbeat
        heartbeat.touch(BEAT_NAME, str(Path(logs) / BEATS_DIR), now if now is not None else time.time())
        return True
    except Exception:
        return False


def read_payload():
    """The hook JSON from stdin read as UTF-8 bytes. {} on any problem."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}                                  # run by hand in a terminal: nothing to read, do not wait
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def handle(payload, logs=None, now=None):
    """-> True when the heartbeat was touched. Never raises."""
    try:
        logs = Path(logs) if logs else logs_dir()
        if (logs / OFF_NAME).exists():
            return False
        if is_subagent_call(payload):
            return False
        return beat(logs, now)
    except Exception:
        return False


def main():
    try:
        handle(read_payload())
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
