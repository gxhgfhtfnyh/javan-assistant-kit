# -*- coding: utf-8 -*-
"""Condense daily notes: file past months' daily notes into month folders and write one "Highlights" note per month.

1. Every note named YYYY-MM-DD.md that sits directly in vault/01 - Daily Notes/ and belongs to a month BEFORE the current one moves into a
   subfolder named "MM - Month YYYY" (for example "09 - September 2026"). The current month's notes, anything not named like a date, and notes edited in
   the last 48 hours (a late-night session may still be appending to yesterday's note) stay where they are. A note is never overwritten: if the
   destination already exists it is left in place and reported. Wiki links by name ([[2026-09-01]]) keep working after the move; links written as a path do not.
2. For each month it filed, it writes 04 - Archive/Monthly Highlights/Highlights - <Month> <Year>.md: each day's own "## Index" bullets, or, when a day has no
   Index section, the lines under "What got done:" in its session blocks (the layout of the kit's daily-note template). Mechanical extraction, nothing
   invented, nothing summarised; a day with neither contributes nothing, and a month with nothing to say gets no note. The highlights live in the archive
   folder, not in the daily folder, so the vault check does not mistake them for a badly named daily note.
The raw daily notes are moved, never edited or deleted.

    python condense_daily_notes.py                    # DRY RUN (the default): print what would move and what would be written, change nothing
    python condense_daily_notes.py --apply            # do it
    python condense_daily_notes.py --min-idle-hours 0 # also move notes edited in the last 48 hours
    python condense_daily_notes.py --apply --job      # for the scheduler: honours logs/condense_daily_notes_off.txt, always exits 0, leaves one alert line
Exit 0; exit 1 if a move failed; exit 2 if the daily-notes folder does not exist. Paths: the vault is HARNESS_VAULT (default ROOT/vault); logs HARNESS_LOGS.
"""
import argparse
import datetime
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

DAILY_FOLDER = "01 - Daily Notes"
HIGHLIGHTS_PARTS = ("04 - Archive", "Monthly Highlights")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")
MONTH_FOLDER_RE = re.compile(r"^(\d{2}) - ([A-Za-z]+) (\d{4})$")
INDEX_HEAD = re.compile(r"^##[ \t]+index[ \t]*#*[ \t]*$", re.I)
H1_H2 = re.compile(r"^#{1,2}[ \t]")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
DONE_LABEL = re.compile(r"^(\s*)[-*+][ \t]*what got done[ \t]*:[ \t]*(.*)$", re.I)
EMPTY_BULLET = re.compile(r"^[-*+]?[ \t]*$")
PLACEHOLDER = re.compile(r"^[-*+]?[ \t]*\(.*\)[ \t]*$")
MIN_IDLE_HOURS = 48.0
MAX_NOTE_BYTES = 2_000_000
OFF_NAME = "condense_daily_notes_off.txt"


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _vault():
    return Path(os.environ.get("HARNESS_VAULT") or _root() / "vault").resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


def daily_dir():
    return _vault() / DAILY_FOLDER


def highlights_dir():
    return _vault().joinpath(*HIGHLIGHTS_PARTS)


def month_folder_name(year, month):
    return "%02d - %s %d" % (month, MONTHS[month - 1], year)


def highlights_name(year, month):
    return "Highlights - %s %d.md" % (MONTHS[month - 1], year)


def date_of_name(name):
    """(year, month, day) for a file name like 2026-09-01.md, or None when it is not a real calendar date."""
    m = DATE_RE.match(name)
    if not m:
        return None
    try:
        datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _write_text_atomic(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(".%s.tmp.%d" % (p.name, os.getpid()))
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, p)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


# ---------- step 1: which notes move ----------
def find_notes_to_roll_up(daily=None, today=None, now=None, min_idle_hours=MIN_IDLE_HOURS):
    """-> (to_roll, too_recent). to_roll: [(path, year, month)] of date-named notes directly in the daily folder from a month before `today`'s that have
    been idle for `min_idle_hours`; too_recent: the names of the otherwise eligible notes edited more recently than that. Sorted by name."""
    daily = Path(daily) if daily else daily_dir()
    today = today or datetime.date.today()
    now = time.time() if now is None else now
    to_roll, too_recent = [], []
    try:
        entries = sorted(os.scandir(daily), key=lambda e: e.name)
    except OSError:
        return [], []
    for e in entries:
        parts = date_of_name(e.name)
        if not parts or e.is_symlink() or not e.is_file():
            continue
        if (parts[0], parts[1]) >= (today.year, today.month):
            continue
        try:
            idle = now - e.stat().st_mtime
        except OSError:
            continue
        if idle < min_idle_hours * 3600:
            too_recent.append(e.name)
        else:
            to_roll.append((Path(e.path), parts[0], parts[1]))
    return to_roll, too_recent


def roll_up_old_notes(apply=False, daily=None, today=None, now=None, min_idle_hours=MIN_IDLE_HOURS):
    """Move (apply=True) or just plan the move of old daily notes into month folders.
    -> {"moves": [(name, "MM - Month YYYY/name", status)], "too_recent": [names]}, status one of 'moved', 'would move', 'exists' (destination taken, left in place), 'error: ...'."""
    daily = Path(daily) if daily else daily_dir()
    to_roll, too_recent = find_notes_to_roll_up(daily, today, now, min_idle_hours)
    moves = []
    for path, year, month in to_roll:
        folder = month_folder_name(year, month)
        dest = daily / folder / path.name
        rel = folder + "/" + path.name
        if dest.exists():
            moves.append((path.name, rel, "exists"))
        elif not apply:
            moves.append((path.name, rel, "would move"))
        else:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.rename(path, dest)
                moves.append((path.name, rel, "moved"))
            except OSError as e:
                moves.append((path.name, rel, "error: %s" % (e.strerror or type(e).__name__)))
    return {"moves": moves, "too_recent": too_recent}


# ---------- step 2: the highlights ----------
def _outside_fences(lines):
    marker = None
    for line in lines:
        m = FENCE.match(line)
        if marker is None:
            if m:
                marker = (m.group(1)[0], len(m.group(1)))
            else:
                yield line
        elif m and m.group(1)[0] == marker[0] and len(m.group(1)) >= marker[1] and not line.strip().strip("`~"):
            marker = None


def _indent(line):
    """Width of the leading whitespace, a tab counting as four spaces."""
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _is_filler(stripped):
    return bool(EMPTY_BULLET.match(stripped) or PLACEHOLDER.match(stripped))


def extract_index_bullets(text):
    """The lines under a "## Index" heading (up to the next # or ## heading), without empty bullets and "(placeholder)" lines. "" when there is none."""
    out, inside = [], False
    for line in _outside_fences(text.splitlines()):
        if inside and H1_H2.match(line):
            break
        if not inside:
            inside = bool(INDEX_HEAD.match(line.strip()))
            continue
        if not _is_filler(line.strip()):
            out.append(line.rstrip())
    return "\n".join(out).strip("\n")


def extract_session_highlights(text):
    """The lines under every "What got done:" label (the kit's session-block layout), inline text and nested bullets, flattened to bullets. "" when there are none."""
    lines = list(_outside_fences(text.splitlines()))
    out, i = [], 0
    while i < len(lines):
        m = DONE_LABEL.match(lines[i])
        i += 1
        if not m:
            continue
        indent = _indent(m.group(1) + "x")
        rest = m.group(2).strip()
        if rest and not _is_filler(rest):
            out.append("- " + rest)
        while i < len(lines):
            line = lines[i]
            if line.strip() and _indent(line) <= indent:
                break
            i += 1
            s = line.strip()
            if s and not _is_filler(s):
                out.append(s if re.match(r"^[-*+][ \t]", s) else "- " + s)
    return "\n".join(out)


def extract_highlights(text):
    """A day's own Index bullets, else its "What got done" lines, else ""."""
    return extract_index_bullets(text) or extract_session_highlights(text)


def _read_note_text(path):
    try:
        if Path(path).stat().st_size > MAX_NOTE_BYTES:
            return ""
        return Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def month_notes(year, month, daily=None, extra=()):
    """Date-named notes of a month: those in its month folder plus `extra` paths (notes about to move, for a dry run). Sorted by name, one per name."""
    daily = Path(daily) if daily else daily_dir()
    found = {}
    folder = daily / month_folder_name(year, month)
    try:
        for e in os.scandir(folder):
            if date_of_name(e.name) and e.is_file() and not e.is_symlink():
                found[e.name] = Path(e.path)
    except OSError:
        pass
    for p in extra:
        p = Path(p)
        if date_of_name(p.name):
            found.setdefault(p.name, p)
    return [found[k] for k in sorted(found)]


def build_highlights_text(year, month, notes):
    """-> (text, days) for the Highlights note of a month, or ("", 0) when no day has anything to say."""
    sections = []
    for note in notes:
        bullets = extract_highlights(_read_note_text(note))
        if bullets:
            sections.append("### [[%s]]\n%s" % (note.stem, bullets))
    if not sections:
        return "", 0
    head = ("---\nstatus: active\ntype: highlights\n---\n# Highlights - %s %d\n\n"
            "Condensed from each day's own `## Index` section (or, when a day has none, its \"What got done\" lines): mechanical extraction, nothing invented. "
            "Full detail is in each day's own note.\n\n" % (MONTHS[month - 1], year))
    return head + "\n\n".join(sections) + "\n", len(sections)


def write_month_highlights(year, month, daily=None, out_dir=None, extra=(), write=True):
    """Build (and, with write=True, save) the Highlights note of a month. -> None when there is nothing to say, else (path, days, status) with status 'written', 'unchanged' or 'would write'."""
    out_dir = Path(out_dir) if out_dir else highlights_dir()
    text, days = build_highlights_text(year, month, month_notes(year, month, daily, extra))
    if not days:
        return None
    path = out_dir / highlights_name(year, month)
    try:
        if path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n") == text:
            return path, days, "unchanged"
    except OSError:
        pass
    if not write:
        return path, days, "would write"
    _write_text_atomic(path, text)
    return path, days, "written"


def month_folders_without_highlights(daily=None, out_dir=None):
    """(year, month) of every month folder in the daily folder whose Highlights note does not exist yet."""
    daily = Path(daily) if daily else daily_dir()
    out_dir = Path(out_dir) if out_dir else highlights_dir()
    out = []
    try:
        for e in sorted(os.scandir(daily), key=lambda e: e.name):
            m = MONTH_FOLDER_RE.match(e.name)
            if m and e.is_dir() and not e.is_symlink() and 1 <= int(m.group(1)) <= 12 and MONTHS[int(m.group(1)) - 1] == m.group(2):
                ym = (int(m.group(3)), int(m.group(1)))
                if not (out_dir / highlights_name(*ym)).exists():
                    out.append(ym)
    except OSError:
        pass
    return out


def run(apply=False, daily=None, out_dir=None, today=None, now=None, min_idle_hours=MIN_IDLE_HOURS):
    """The whole job. -> {"moves": [...], "too_recent": [...], "highlights": [(path, days, status)]}."""
    daily = Path(daily) if daily else daily_dir()
    plan = roll_up_old_notes(apply, daily, today, now, min_idle_hours)
    pending = {}
    for name, rel, status in plan["moves"]:
        ym = date_of_name(name)[:2]
        if status in ("moved", "would move"):
            pending.setdefault(ym, [])
            if status == "would move":
                pending[ym].append(daily / name)
    months = set(pending) | set(month_folders_without_highlights(daily, out_dir))
    highlights = []
    for ym in sorted(months):
        res = write_month_highlights(ym[0], ym[1], daily, out_dir, extra=pending.get(ym, ()), write=apply)
        if res:
            highlights.append(res)
    plan["highlights"] = highlights
    return plan


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="condense_daily_notes.py", description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="really move the notes and write the highlights (the default only prints the plan)")
    ap.add_argument("--min-idle-hours", type=float, default=MIN_IDLE_HOURS, help="leave notes edited more recently than this alone (default %g; 0 moves everything old)" % MIN_IDLE_HOURS)
    ap.add_argument("--job", action="store_true", help="for the scheduler: honour the kill switch, always exit 0, leave an alert line when something moved")
    a = ap.parse_args(argv)
    if a.job and (_logs() / OFF_NAME).exists():
        print("condense_daily_notes: off (%s present)" % OFF_NAME)
        return 0
    daily = daily_dir()
    if not daily.is_dir():
        print("daily notes folder not found: %s" % daily)
        return 0 if a.job else 2
    res = run(apply=a.apply, daily=daily, min_idle_hours=max(0.0, a.min_idle_hours))
    moves = res["moves"]
    if not moves:
        print("Nothing to roll up - every dated daily note is from the current month or was edited recently.")
    else:
        print(("Moving %d old daily note(s):" if a.apply else "Would move %d old daily note(s) (dry run, nothing changed; --apply to do it):") % len(moves))
        for name, rel, status in moves:
            print("  %s -> %s  [%s]" % (name, rel, status))
    if res["too_recent"]:
        print("Left alone (edited in the last %g h): %s" % (a.min_idle_hours, ", ".join(res["too_recent"])))
    for path, days, status in res["highlights"]:
        print("Highlights %s: %s (%d day(s))" % (status, path, days))
    failed = [m for m in moves if m[2].startswith("error")]
    moved = [m for m in moves if m[2] == "moved"]
    if a.apply and a.job and moved:
        try:
            import notify
            notify.alert("filed %d old daily note(s) into month folders" % len(moved), source="condense_daily_notes", logs=str(_logs()))
        except Exception:                                   # noqa: BLE001 - the alert line is a courtesy
            pass
    return 1 if failed and not a.job else 0


if __name__ == "__main__":
    sys.exit(main())
