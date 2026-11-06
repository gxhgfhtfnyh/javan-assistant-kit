# -*- coding: utf-8 -*-
"""Stop hook for Claude Code.

Reads hook JSON from stdin (fields: stop_hook_active).
Touches heartbeat 'claude_session'.
When logs/stop_gate_off.txt exists -> allows.
When stop_hook_active is true -> allows (prevents loops).
When an actionable tier 0/1 intent of value >= 3 is waiting -> blocks with JSON:
  {"decision": "block", "reason": "..."}
  Reason tells assistant to claim it (python scripts/intent_store.py claim <id> --by live-session),
  do the work, and close it with done <id> --result ok|fail|skipped --evidence '...'.
Blocks at most 3 times per hour (counter in logs/stop_gate_state.json).
After 3 blocks per hour, allows and appends line to logs/alerts.txt.
Otherwise prints nothing.

Pure functions: brief_line(), should_block().
Run: python stop_gate.py
"""
import json
import os
import sys
import time
from pathlib import Path

# Root and paths
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

STATE_FILE = LOGS / "stop_gate_state.json"
OFF_FILE = LOGS / "stop_gate_off.txt"
ALERTS_FILE = LOGS / "alerts.txt"
MAX_BLOCKS_PER_HOUR = 3
MIN_VALUE = 3


def touch_heartbeat() -> None:
    """Touch the claude_session heartbeat."""
    try:
        import heartbeat
        heartbeat.touch("claude_session", str(LOGS / "heartbeats"))
    except Exception:
        pass


def read_stdin() -> dict:
    """Read and parse JSON from stdin. Returns empty dict on error."""
    try:
        data = sys.stdin.read()
        if not data.strip():
            return {}
        return json.loads(data)
    except Exception:
        return {}


def load_state() -> dict:
    """Load state from logs/stop_gate_state.json."""
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    """Save state to logs/stop_gate_state.json."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def blocks_this_hour(state: dict, now: float) -> list[float]:
    """Return timestamps of blocks within the last hour."""
    blocks = state.get("blocks", [])
    return [t for t in blocks if now - t < 3600]


def record_block(state: dict, now: float) -> dict:
    """Add a block timestamp to state and return updated state."""
    blocks = blocks_this_hour(state, now)
    blocks.append(now)
    state["blocks"] = blocks
    return state


def append_alert(message: str) -> None:
    """Append a line to logs/alerts.txt."""
    try:
        ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with ALERTS_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%F %T')} {message}\n")
    except Exception:
        pass


def brief_line() -> str | None:
    """Return the brief next-action line from intent_store, or None if no actionable intent."""
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "intent_store.py"), "next"],
            cwd=SCRIPTS,
            capture_output=True,
            text=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),  # the child prints notes with accents and dashes; a narrow pipe encoding would make it fail silently
        )
        line = (result.stdout or "").strip()
        if line and not line.startswith("NONE"):
            return line
        return None
    except Exception:
        return None


def should_block(hook_data: dict, brief: str | None = None) -> tuple[bool, str]:
    """
    Determine if the stop should be blocked.

    Returns (block, reason). If block is False, reason explains why not.
    """
    # Kill switch
    if OFF_FILE.exists():
        return False, "stop_gate_off.txt exists"

    # Already continuing via another hook -> never loop
    if hook_data.get("stop_hook_active"):
        return False, "stop_hook_active is true"

    # Get the brief line if not provided
    if brief is None:
        brief = brief_line()

    # No actionable intent waiting
    if not brief:
        return False, "no actionable intent"

    # Parse the brief line to extract intent id, tier, value
    # Format: "NEXT <id> tier<t> value<v> <what>"
    import re
    match = re.match(r"NEXT\s+(\S+)\s+tier(\d+)\s+value(\d+)\s+(.+)", brief)
    if not match:
        return False, "could not parse brief line"

    intent_id, tier_str, value_str, what = match.groups()
    tier = int(tier_str)
    value = int(value_str)

    # Only block on tier 0 or 1 with value >= MIN_VALUE
    if tier not in (0, 1):
        return False, f"tier {tier} not actionable (only 0/1)"

    if value < MIN_VALUE:
        return False, f"value {value} below threshold {MIN_VALUE}"

    # Check hourly block limit
    state = load_state()
    now = time.time()
    recent_blocks = blocks_this_hour(state, now)

    if len(recent_blocks) >= MAX_BLOCKS_PER_HOUR:
        # Hourly cap reached - allow, and log one alert per hour (not one per stop attempt)
        if now - float(state.get("last_cap_alert", 0)) >= 3600:
            append_alert(f"stop_gate hourly cap reached ({MAX_BLOCKS_PER_HOUR}/hour), allowing stop. Intent {intent_id} was waiting.")
            state["last_cap_alert"] = now
            save_state(state)
        return False, "hourly cap reached"

    # Should block
    reason = (
        f"Actionable intent {intent_id} (tier {tier}, value {value}) is waiting: {what}. "
        f"Claim it now: python scripts/intent_store.py claim {intent_id} --by live-session. "
        f"Do the work, then close it: python scripts/intent_store.py done {intent_id} --result ok|fail|skipped --evidence '...'"
    )
    return True, reason


def main() -> None:
    """Entry point: read stdin, check conditions, maybe block."""
    try:
        touch_heartbeat()
        hook_data = read_stdin()

        # Check if we should block
        block, reason = should_block(hook_data)

        if block:
            state = load_state()
            now = time.time()
            state = record_block(state, now)
            save_state(state)

            output = json.dumps({"decision": "block", "reason": reason})  # ASCII-escaped on purpose: a Windows pipe may not be UTF-8 and a failed write would silently allow the stop
            sys.stdout.write(output)
        # If not blocking, print nothing (empty stdout = allow)
    except Exception:
        # Never raise - just allow
        pass


if __name__ == "__main__":
    main()