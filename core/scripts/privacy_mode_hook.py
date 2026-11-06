# -*- coding: utf-8 -*-
"""PreToolUse hook: while privacy mode is on, refuse tools that take a picture of the screen.

Wire it with matcher "Bash|PowerShell|mcp__.*". Claude Code pipes {"tool_name": ..., "tool_input": {...}} to stdin. ALLOW = print nothing, exit 0.
DENY = print one JSON object {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": ...}} and exit 0.

What is refused while privacy mode is on:
  * MCP tools whose name says they take a screenshot (any server: ...screenshot..., screen_capture) and the desktop-state tool "Snapshot" of the windows-mcp server;
  * MCP tools called with {"action": "screenshot"} (browser extensions that put the screenshot in an action field);
  * a shell command that tries to switch privacy mode off (the assistant must not do that on its own; the user types it with the ! prefix in the terminal).
Everything else passes. desktop_screenshot.py checks the switch itself, so it needs no entry here.

It FAILS OPEN on any error (a broken hook must not break the turn) and has a kill switch: the file logs/privacy_mode_hook_off.txt.
This is a guard against casual or injected capture, not a sandbox: a program that grabs the screen on its own is not seen.
Environment: HARNESS_ROOT, HARNESS_LOGS. Pure core (unit tested): captures_screen, tries_to_switch_off, decide, deny_json."""
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

OFF_NAME = "privacy_mode_hook_off.txt"
SCREEN_WORDS = ("screenshot", "screen_capture", "screencapture", "capture_screen")
SCREEN_ACTIONS = ("screenshot", "screen_capture", "capture")
SNAPSHOT_SERVERS = ("mcp__windows-mcp__",)


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


def captures_screen(tool_name, tool_input):
    """True when this MCP tool call would put a picture of the screen in front of the assistant."""
    t = str(tool_name or "").lower()
    if not t.startswith("mcp__"):
        return False
    last = t.rsplit("__", 1)[-1]
    if any(w in last for w in SCREEN_WORDS):
        return True
    if last == "snapshot" and t.startswith(SNAPSHOT_SERVERS):
        return True
    if isinstance(tool_input, dict):
        action = tool_input.get("action")
        if isinstance(action, str) and action.strip().lower() in SCREEN_ACTIONS:
            return True
    return False


def tries_to_switch_off(tool_name, tool_input):
    """True for a shell command that runs privacy_mode with `off` or edits its switch file."""
    if str(tool_name or "").lower() not in ("bash", "powershell"):
        return False
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(cmd, str):
        return False
    low = cmd.lower()
    if "privacy_mode.json" in low or OFF_NAME in low:
        return True
    return "privacy_mode" in low and re.search(r"\boff\b", low) is not None


def decide(payload, active):
    """-> (blocked, reason). `active` says whether privacy mode is on."""
    if not active or not isinstance(payload, dict):
        return False, ""
    tool, tin = payload.get("tool_name"), payload.get("tool_input")
    if captures_screen(tool, tin):
        return True, "Privacy mode is on, so screen capture is blocked. Do not look for a workaround. If a picture is really needed, ask the user to turn privacy mode off."
    if tries_to_switch_off(tool, tin):
        return True, "Privacy mode is on and only the user switches it off. Ask them to run it themselves in the terminal: python scripts/privacy_mode.py off"
    return False, ""


def deny_json(reason):
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}})


def read_payload():
    """The hook JSON from stdin read as UTF-8 bytes. {} on any problem."""
    try:
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def handle(payload, logs=None):
    """-> the JSON text to print, or '' to allow. Never raises."""
    try:
        logs = Path(logs) if logs else _logs()
        if (logs / OFF_NAME).exists():
            return ""
        import privacy_mode
        blocked, reason = decide(payload, privacy_mode.is_active(logs))
        return deny_json(reason) if blocked else ""
    except Exception:
        return ""


def main():
    try:
        out = handle(read_payload())
        if out:
            sys.stdout.write(out + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
