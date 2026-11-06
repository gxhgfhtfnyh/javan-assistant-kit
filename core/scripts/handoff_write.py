# -*- coding: utf-8 -*-
"""Stop hook for Claude Code: writes logs/handoff.md.

One short file (hard cap 2500 characters) holding what a fresh session needs to resume after a
crash or a /compact:
  - the time it was written
  - the focus file logs/focus.md (capped)
  - intents claimed and in flight, then the top open intents from intent_store (ranked), plus a
    count of tier-2 intents waiting for the owner
  - the last 5 lines of logs/alerts.txt, if present

Reads hook JSON from stdin (drained, not needed) and prints nothing, so the stop is never blocked.
Writes atomically: a temp file next to the target, then os.replace, so a reader never sees half a file.
When logs/handoff_off.txt exists -> does nothing. Never raises. Read-only toward the intent store.

Pure functions: clip(), tail_lines(), focus_section(), intent_lines(), alert_lines(), build_handoff().
Run: python handoff_write.py
"""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

# Root and paths
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

HANDOFF_FILE = LOGS / "handoff.md"
OFF_FILE = LOGS / "handoff_off.txt"
FOCUS_FILE = LOGS / "focus.md"
ALERTS_FILE = LOGS / "alerts.txt"

MAX_CHARS = 2500          # hard cap on the whole file
FOCUS_CAP = 800           # characters of the focus file kept
INTENTS_SHOWN = 5         # top ranked open intents listed
DOING_SHOWN = 3           # in-flight (claimed) intents listed first
INTENT_LINE_CAP = 150
ALERTS_SHOWN = 5
ALERT_LINE_CAP = 140
TRUNCATED_MARK = "... [truncated]"


def read_stdin() -> dict:
    """Read hook JSON from stdin as UTF-8 bytes. Returns {} on a terminal, empty input or any error."""
    try:
        stream = sys.stdin
        if stream is None or (hasattr(stream, "isatty") and stream.isatty()):
            return {}
        buf = getattr(stream, "buffer", None)
        raw = buf.read() if buf is not None else stream.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not raw or not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_text(path: Path) -> str:
    """Whole file as stripped text with LF newlines, or '' on any error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").strip()
    except Exception:
        return ""


def one_line(text) -> str:
    """Collapse all whitespace (including newlines) to single spaces."""
    return " ".join(str(text if text is not None else "").split())


def clip(text: str, limit: int) -> str:
    """Return text of at most `limit` characters; a longer text is cut (at a word break when one is near) and ends with '...'."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: max(limit - 3, 0)]
    brk = max(cut.rfind("\n"), cut.rfind(" "))
    if brk > limit * 0.6:
        cut = cut[:brk]
    return cut.rstrip() + "..."


def tail_lines(path: Path, count: int, max_bytes: int = 16384) -> list[str]:
    """Last `count` non-empty lines of a text file, reading only its final max_bytes. [] when missing or unreadable."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            raw = f.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        if start > 0 and lines:
            lines = lines[1:]  # the first line may have been cut in the middle
        lines = [ln.strip() for ln in lines if ln.strip()]
        return lines[-count:] if count > 0 else []
    except Exception:
        return []


def focus_section() -> str:
    """The focus file capped at FOCUS_CAP characters, or a placeholder."""
    return clip(read_text(FOCUS_FILE), FOCUS_CAP) or "(no focus file)"


def _intent_line(intent: dict, prefix: str = "") -> str:
    text = "- %s%s tier%s value%s %s" % (
        prefix, intent.get("id", "?"), intent.get("tier", "?"), intent.get("value", "?"), one_line(intent.get("what")))
    return clip(text, INTENT_LINE_CAP)


def intent_lines(now: float | None = None) -> list[str]:
    """Lines for the intents section: claimed intents first (marked 'doing'), then the top ranked open ones,
    then a count of tier-2 intents waiting on the owner. Uses intent_store's own read_jsonl, expire_leases and rank;
    nothing is written back to the store."""
    now = time.time() if now is None else now
    try:
        import intent_store
        rows = intent_store.read_jsonl(intent_store.INTENTS, logs=str(LOGS))
        intents = [i for i in rows if isinstance(i, dict) and i.get("id")]
        intent_store.expire_leases(intents, now)  # in memory only: a dead run must not look in flight
        doing = sorted((i for i in intents if i.get("status") == "doing"), key=lambda i: float(i.get("epoch") or 0))
        ranked = intent_store.rank(intents, now)
        waiting = sum(1 for i in intents if i.get("status") in ("open", "waiting") and int(i.get("tier", 1)) == 2)
    except Exception:
        return ["(intent store unavailable)"]
    lines = [_intent_line(i, "doing ") for i in doing[:DOING_SHOWN]]
    lines += [_intent_line(i) for i in ranked[:INTENTS_SHOWN]]
    if waiting:
        lines.append("- %d waiting on the owner (tier 2)" % waiting)
    return lines or ["(none open)"]


def alert_lines() -> list[str]:
    """The last ALERTS_SHOWN lines of logs/alerts.txt, each capped, or a placeholder."""
    lines = [clip(one_line(ln), ALERT_LINE_CAP) for ln in tail_lines(ALERTS_FILE, ALERTS_SHOWN)]
    return ["- " + ln for ln in lines] or ["(none)"]


def hard_cap(text: str, limit: int = MAX_CHARS) -> str:
    """Whole-file guard: never more than `limit` characters; the cut falls on a line end and is marked."""
    if len(text) <= limit:
        return text
    room = limit - len(TRUNCATED_MARK) - 2
    cut = text[:room]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut.rstrip() + "\n" + TRUNCATED_MARK + "\n"


def build_handoff(now: float | None = None) -> str:
    """The full handoff text. Each part is read defensively, so one broken source never loses the others."""
    now = time.time() if now is None else now
    stamp = dt.datetime.fromtimestamp(now).replace(microsecond=0).isoformat()
    parts = [
        "# Handoff (written %s)" % stamp,
        "",
        "Read this to resume after a crash or /compact.",
        "",
        "## Focus",
        focus_section(),
        "",
        "## Open intents (claimed first, then top ranked)",
    ]
    parts += intent_lines(now)
    parts += ["", "## Last alerts"]
    parts += alert_lines()
    return hard_cap("\n".join(parts) + "\n")


def write_atomic(path: Path, text: str) -> None:
    """Write text to path via a temp file in the same folder, then os.replace. On any failure the temp file is
    removed, the old file is left as it was, and the error is raised to the caller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.tmp.%d" % (path.name, os.getpid()))
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        for attempt in range(6):  # a reader holding the file open makes os.replace raise PermissionError for a moment on Windows
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    """Entry point: drain stdin, honour the kill switch, write the handoff. Never raises, prints nothing."""
    try:
        read_stdin()
        if OFF_FILE.exists():
            return 0
        write_atomic(HANDOFF_FILE, build_handoff())
    except Exception:
        # Never raise: a failed handoff must not break the stop
        pass
    return 0


if __name__ == "__main__":
    main()
