"""Heartbeat module for tracking service heartbeats and restarts."""

import time
from pathlib import Path


def touch(name: str, d: str | Path, now: float | None = None) -> None:
    """Write a beat file containing the unix time as text."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    if now is None:
        now = time.time()
    beat_file = d / f"{name}.beat"
    beat_file.write_text(str(now))


def age_seconds(name: str, d: str | Path, now: float | None = None) -> float | None:
    """Return seconds since the beat file was written, or None if missing/invalid."""
    d = Path(d)
    beat_file = d / f"{name}.beat"
    if not beat_file.exists():
        return None
    try:
        content = beat_file.read_text().strip()
        beat_time = float(content)
    except (ValueError, OSError):
        return None
    if now is None:
        now = time.time()
    return now - beat_time


def stale(names: list[str], max_age: float, d: str | Path, now: float | None = None) -> list[str]:
    """Return sorted list of names whose age is None or greater than max_age."""
    result = []
    for name in names:
        age = age_seconds(name, d, now)
        if age is None or age > max_age:
            result.append(name)
    return sorted(result)


def record_restart(name: str, d: str | Path, now: float | None = None) -> None:
    """Append the time to the restart file (one number per line)."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    if now is None:
        now = time.time()
    restart_file = d / f"{name}.restarts"
    with restart_file.open("a") as f:
        f.write(f"{now}\n")


def restart_allowed(
    name: str,
    max_restarts: int,
    window_seconds: float,
    d: str | Path,
    now: float | None = None
) -> bool:
    """Return True if fewer than max_restarts fall within the last window_seconds."""
    d = Path(d)
    restart_file = d / f"{name}.restarts"
    if not restart_file.exists():
        return True
    if now is None:
        now = time.time()
    try:
        content = restart_file.read_text().strip()
        if not content:
            return True
        restart_times = [float(line) for line in content.splitlines() if line.strip()]
    except (ValueError, OSError):
        return True
    cutoff = now - window_seconds
    recent_restarts = sum(1 for t in restart_times if t >= cutoff)
    return recent_restarts < max_restarts