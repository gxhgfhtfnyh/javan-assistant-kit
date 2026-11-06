# -*- coding: utf-8 -*-
"""How often the installed skills actually get loaded, and whether a nudge made a difference.

    python scripts/skill_use.py                 # today's count, the last 7 days, the most used skills, and how many nudged skills were loaded afterwards
    python scripts/skill_use.py --days 14       # a longer window
    python scripts/skill_use.py --dir <folder>  # read the transcripts from this folder instead

It reads Claude Code's session transcripts (read only, nothing is written or sent) and counts every call of the Skill tool. Claude Code keeps the
transcripts of a project in <config folder>/projects/<project folder path with every character that is not a letter or digit turned into "-">/*.jsonl;
the config folder is CLAUDE_CONFIG_DIR or ~/.claude, and the project folder is this assistant's folder. If your transcripts live somewhere else,
pass --dir or set HARNESS_TRANSCRIPTS. At most the newest 300 transcript files are read.

If skill_nudge.py has been nudging (logs/skill_nudge.jsonl exists), it also reports how many nudged skills were loaded within 15 minutes after the
nudge: the number that tells you whether the nudges help. A missing log is fine; that line is simply left out.
Days are local calendar days (the transcripts store UTC and are converted). Exit code 0 even when nothing is found.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_TRANSCRIPTS (optional transcripts folder).
Pure helpers (unit tested): project_slug, transcripts_dir, invocations, count_by_day, count_by_skill, followed, read_nudges."""
import argparse
import calendar
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_FILES = 300
MAX_LINE_CHARS = 5_000_000        # a longer transcript line is skipped (a huge pasted blob), the rest of the file is still read


def root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent)


def logs_dir():
    return Path(os.environ.get("HARNESS_LOGS") or root() / "logs")


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def project_slug(path):
    """The folder name Claude Code uses for a project's transcripts: every character that is not a letter or digit becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def transcripts_dir(project_root=None, config=None):
    """The folder holding this project's transcripts (it may not exist). HARNESS_TRANSCRIPTS wins; else a case-insensitive match of the slug."""
    override = os.environ.get("HARNESS_TRANSCRIPTS")
    if override:
        return Path(override)
    projects = Path(config) / "projects" if config else config_dir() / "projects"
    slug = project_slug(project_root if project_root is not None else root().resolve())
    exact = projects / slug
    if exact.is_dir():
        return exact
    try:
        for d in projects.iterdir():
            if d.is_dir() and d.name.lower() == slug.lower():
                return d
    except OSError:
        pass
    return exact


def transcript_files(folder, limit=MAX_FILES):
    """The newest `limit` *.jsonl files of a folder, as a list of paths."""
    try:
        files = [p for p in Path(folder).glob("*.jsonl") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:limit]
    except OSError:
        return []


def invocations(paths):
    """[(iso timestamp, skill name)] for every Skill tool call in the given transcript files. Unreadable files and bad lines are skipped."""
    out = []
    for f in paths:
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"Skill"' not in line or len(line) > MAX_LINE_CHARS:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    msg = r.get("message") if isinstance(r, dict) else None
                    content = msg.get("content") if isinstance(msg, dict) else None
                    if not isinstance(content, list):
                        continue
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                            out.append((str(r.get("timestamp") or ""), str(inp.get("skill") or "?")))
        except OSError:
            continue
    return out


def utc_epoch(iso):
    """Epoch seconds of a transcript time stamp like 2026-10-29T01:00:00.123Z (UTC); 0.0 when it cannot be read."""
    try:
        return float(calendar.timegm(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, OverflowError):
        return 0.0


def local_day(iso):
    """The local calendar day (YYYY-MM-DD) of a transcript time stamp; '' when it cannot be read."""
    e = utc_epoch(iso)
    return time.strftime("%Y-%m-%d", time.localtime(e)) if e else ""


def count_by_day(rows):
    """{local day: number of invocations}"""
    d = {}
    for ts, _ in rows:
        day = local_day(ts)
        if day:
            d[day] = d.get(day, 0) + 1
    return d


def count_by_skill(rows):
    """{skill name: number of invocations}"""
    d = {}
    for _, skill in rows:
        d[skill] = d.get(skill, 0) + 1
    return d


def followed(nudges, invs, window_s=900):
    """(nudged, followed): a nudge counts as followed when that skill was invoked within window_s after it.
    nudges = [(local 'YYYY-mm-dd HH:MM:SS', [skills])], invs = [(UTC iso, skill)]."""
    by_skill = {}
    for ts, sk in invs:
        by_skill.setdefault(sk, []).append(utc_epoch(ts))
    n = f = 0
    for stamp, skills in nudges:
        try:
            t = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            continue
        for s in skills:
            n += 1
            if any(0 <= e - t <= window_s for e in by_skill.get(s, [])):
                f += 1
    return n, f


def read_nudges(path):
    """[(local time stamp, [skills])] from logs/skill_nudge.jsonl; [] when the file is missing."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    if isinstance(r, dict) and isinstance(r["ts"], str) and isinstance(r["skills"], list):
                        out.append((r["ts"], [str(s) for s in r["skills"]]))
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    return out


def report(rows, nudges, days=7, today=None):
    """The text printed by main(), as a list of lines."""
    by_day = count_by_day(rows)
    today = today or time.strftime("%Y-%m-%d")
    lines = ["skill invocations today (%s): %d" % (today, by_day.get(today, 0))]
    recent = sorted(by_day.items())[-max(1, days):]
    lines.append("last %d day(s) with any: %s" % (len(recent), ", ".join("%s=%d" % kv for kv in recent) if recent else "none"))
    top = sorted(count_by_skill(rows).items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    lines.append("most used: " + (", ".join("%s=%d" % kv for kv in top) if top else "none"))
    if nudges:
        n, f = followed(nudges, rows)
        lines.append("nudges: %d, followed by that skill within 15 min: %d" % (n, f))
    return lines


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Count Skill tool calls in Claude Code transcripts.")
    ap.add_argument("--days", type=int, default=7, help="how many days with activity to list (default 7)")
    ap.add_argument("--dir", default="", help="transcripts folder (default: found from the assistant folder)")
    args = ap.parse_args(argv)
    folder = Path(args.dir) if args.dir else transcripts_dir()
    files = transcript_files(folder)
    if not files:
        print("no transcripts found in %s (pass --dir <folder> or set HARNESS_TRANSCRIPTS)" % folder)
        return 0
    rows = invocations(files)
    for line in report(rows, read_nudges(logs_dir() / "skill_nudge.jsonl"), args.days):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
