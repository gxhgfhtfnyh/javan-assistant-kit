# -*- coding: utf-8 -*-
"""SessionStart hook for Claude Code.

Prints context at session start:
- First 60 lines of vault/VAULT-INDEX.md
- First 40 lines of vault/Active Priorities.md
- Wake line from intent_store (the 'next' command output)
- Count of open tier-2 intents waiting for owner
- Contents of logs/boot_reminders.txt if present
- Touches heartbeat 'claude_session'
Never raises, prints at most 8000 characters.

Run: python session_start.py
"""
import json
import os
import sys
from pathlib import Path

# Root and paths
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

SCRIPTS = Path(__file__).parent.resolve()
MAX_CHARS = 8000


def read_lines(path: Path, max_lines: int) -> list[str]:
    """Read up to max_lines from a file, return empty list on any error."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = []
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n"))
            return lines
    except Exception:
        return []


def get_vault_index() -> list[str]:
    """First 60 lines of VAULT-INDEX.md."""
    return read_lines(VAULT / "VAULT-INDEX.md", 60)


def get_active_priorities() -> list[str]:
    """First 40 lines of Active Priorities.md."""
    return read_lines(VAULT / "Active Priorities.md", 40)


def get_intent_wake_line() -> str:
    """Run intent_store.next and return the output line (or empty string)."""
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
        if line:
            return line
        return (result.stderr or "").strip()
    except Exception:
        return ""


def count_tier2_waiting() -> int:
    """Count open intents with tier == 2."""
    try:
        import intent_store
        intents = intent_store.read_jsonl(intent_store.INTENTS, logs=str(LOGS))
        return sum(1 for i in intents if i.get("status") in ("open", "waiting") and int(i.get("tier", 1)) == 2)
    except Exception:
        return 0


def get_boot_reminders() -> str:
    """Contents of logs/boot_reminders.txt if present, else empty string."""
    path = LOGS / "boot_reminders.txt"
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def touch_heartbeat() -> None:
    """Touch the claude_session heartbeat."""
    try:
        import heartbeat
        heartbeat.touch("claude_session", str(LOGS / "heartbeats"))
    except Exception:
        pass


def build_output() -> str:
    """Assemble the full output, truncated to MAX_CHARS."""
    parts = []

    vault_index = get_vault_index()
    if vault_index:
        parts.append("=== VAULT-INDEX.md (first 60 lines) ===")
        parts.extend(vault_index)

    active = get_active_priorities()
    if active:
        parts.append("\n=== Active Priorities.md (first 40 lines) ===")
        parts.extend(active)

    wake = get_intent_wake_line()
    if wake:
        parts.append(f"\n=== Intent wake line ===")
        parts.append(wake)

    tier2_count = count_tier2_waiting()
    parts.append(f"\n=== Tier-2 waiting for owner: {tier2_count} ===")

    boot = get_boot_reminders()
    if boot:
        parts.append("\n=== boot_reminders.txt ===")
        parts.append(boot)

    output = "\n".join(parts)
    if len(output) > MAX_CHARS:
        output = output[:MAX_CHARS] + "\n... [truncated]"
    return output


def main() -> None:
    """Entry point: touch heartbeat, print context, never raise."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # notes hold dashes, accents and emoji; a cp1252 pipe would raise and print nothing
    except (AttributeError, ValueError):
        pass
    try:
        touch_heartbeat()
        output = build_output()
        if output:
            sys.stdout.write(output)
    except Exception:
        pass


if __name__ == "__main__":
    main()