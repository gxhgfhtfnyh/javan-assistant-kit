#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token usage report from Claude Code's own local session logs.

Read-only: streams every *.jsonl under <claude config folder>/projects (subagent logs included), never modifies,
moves or deletes anything, no network, standard library only. The folder is ~/.claude, or CLAUDE_CONFIG_DIR when
that is set; --root points at a projects folder directly.

Usage:
  python token_usage_report.py                  weekly window (since your plan's last weekly reset) + today
  python token_usage_report.py --days 7         last N days instead of the weekly window
  python token_usage_report.py --top 15         top N sessions / tools
  python token_usage_report.py --json           machine-readable output

Your time zone and your plan's weekly reset are NOT guessed. Give them as arguments or in logs/usage_config.json:
  {"timezone": "Europe/Paris", "reset_day": "monday", "reset_hour": 10, "reset_minute": 0}
  --tz Europe/Paris --reset-day monday --reset-hour 10 [--reset-minute 0]      (arguments win over the file)
timezone: an IANA name (Europe/Paris), an offset (+02:00, UTC-5), UTC, or local (the default: this machine's own zone;
          IANA names on Windows need `pip install tzdata`).
reset_day: monday..sunday, mon..sun, or 0..6 with 0 = Monday. reset_hour: 0..23 in that zone. Both must be given together.
Without a reset day and hour there is no weekly window: the report shows a rolling 7 days and says so.

Token columns: fresh input (input_tokens), cache write (cache_creation_input_tokens), cache read
(cache_read_input_tokens), output (output_tokens). Assistant messages are deduplicated by message.id (streaming
repeats them); the largest usage seen per id wins. Tool figures are estimates: each assistant turn's tokens are
split evenly across its tool_use blocks. Exit codes: 0 ok, 1 no projects folder, 2 bad settings or the sums disagree."""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs")

CONFIG_NAME = "usage_config.json"
FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
COLS = ("fresh", "cache_write", "cache_read", "output")
MAX_LINE = 32 * 1024 * 1024          # a transcript line longer than this is skipped, not loaded
DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class ConfigError(ValueError):
    """A setting (argument or logs/usage_config.json) is missing a piece or makes no sense."""


# ---------------------------------------------------------------- where things live

def claude_config_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def default_projects_dir():
    return claude_config_dir() / "projects"


def config_path():
    return LOGS / CONFIG_NAME


# ---------------------------------------------------------------- settings: time zone and weekly reset

def parse_zone(spec):
    """-> (tzinfo, label). tzinfo None means this machine's local zone. Raises ConfigError for anything it cannot understand."""
    s = "" if spec is None else str(spec).strip()
    if not s or s.lower() == "local":
        return None, "local time"
    if s.lower() in ("utc", "z", "gmt"):
        return timezone.utc, "UTC"
    m = re.fullmatch(r"(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", s, re.I)
    if m:
        hours, mins = int(m.group(2)), int(m.group(3) or 0)
        if hours > 14 or mins > 59 or (hours == 14 and mins):
            raise ConfigError("time zone offset %r is out of range (-14:00 .. +14:00)" % s)
        delta = timedelta(hours=hours, minutes=mins)
        if m.group(1) == "-":
            delta = -delta
        return timezone(delta), "UTC%s%02d:%02d" % (m.group(1), hours, mins)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(s), s
    except Exception:
        raise ConfigError("unknown time zone %r (use an IANA name such as Europe/Paris, an offset such as +02:00, UTC or local; "
                          "on Windows IANA names need: pip install tzdata)" % s[:60])


def parse_day(value):
    """monday..sunday, mon..sun or 0..6 (0 = Monday) -> 0..6. Raises ConfigError."""
    if isinstance(value, bool) or value is None:
        raise ConfigError("reset_day must be a weekday name or 0..6 (0 = Monday)")
    s = str(value).strip().lower()
    if s.isdigit() and 0 <= int(s) <= 6:
        return int(s)
    for i, name in enumerate(DAY_NAMES):
        if s == name or (len(s) >= 3 and name.startswith(s)):
            return i
    raise ConfigError("reset_day %r is not a weekday name or a number 0..6 (0 = Monday)" % str(value)[:20])


def parse_time(hour, minute=None):
    """hour 0..23 (or a 'H:MM' string) and an optional minute 0..59 -> (hour, minute). Raises ConfigError."""
    if isinstance(hour, bool) or hour is None:
        raise ConfigError("reset_hour must be a number 0..23")
    try:
        text = str(hour).strip()
        if ":" in text:
            h, m = text.split(":", 1)
            hh, mm = int(h), int(m)
        else:
            hh, mm = int(text), 0
        if minute is not None and not isinstance(minute, bool):
            mm = int(str(minute).strip())
    except ValueError:
        raise ConfigError("reset_hour / reset_minute must be numbers (hour 0..23, minute 0..59)")
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ConfigError("reset time %02d:%02d is outside 00:00..23:59" % (hh, mm))
    return hh, mm


def load_config(path=None):
    """The settings file as a dict. A missing file is an empty dict; a file that is not a JSON object raises ConfigError."""
    p = Path(path) if path else config_path()
    try:
        raw = p.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError("cannot read %s (%s)" % (p.name, type(e).__name__))
    try:
        data = json.loads(raw)
    except ValueError:
        raise ConfigError("%s is not valid JSON" % p.name)
    if not isinstance(data, dict):
        raise ConfigError("%s must hold a JSON object" % p.name)
    return data


def resolve_settings(tz=None, reset_day=None, reset_hour=None, reset_minute=None, cfg=None):
    """Arguments win over the file. -> {"tz": tzinfo|None, "tz_label": str, "reset": (day, hour, minute) or None}."""
    cfg = cfg or {}
    tz_spec = tz if tz not in (None, "") else cfg.get("timezone")
    day = reset_day if reset_day not in (None, "") else cfg.get("reset_day")
    hour = reset_hour if reset_hour not in (None, "") else cfg.get("reset_hour")
    minute = reset_minute if reset_minute not in (None, "") else cfg.get("reset_minute")
    zone, label = parse_zone(tz_spec)
    reset = None
    if day is not None or hour is not None:
        if day is None or hour is None:
            raise ConfigError("reset_day and reset_hour must be given together (weekly reset day and hour)")
        reset = (parse_day(day),) + parse_time(hour, minute)
    elif minute is not None:
        raise ConfigError("reset_minute needs reset_day and reset_hour too")
    return {"tz": zone, "tz_label": label, "reset": reset}


# ---------------------------------------------------------------- time helpers

def parse_ts(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def to_int(x):
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def at_wall(day, hour, minute, tz):
    """The moment of wall-clock hour:minute on calendar date `day` in zone tz (None = this machine's local zone)."""
    naive = datetime(day.year, day.month, day.day, hour, minute)
    return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()


def weekly_start(now, tz, day, hour, minute=0):
    """The most recent weekday `day` (0 = Monday) at hour:minute in zone tz that is at or before `now`."""
    d = now.astimezone(tz)
    anchor = d.date() - timedelta(days=(d.weekday() - day) % 7)
    start = at_wall(anchor, hour, minute, tz)
    if start > d:
        start = at_wall(anchor - timedelta(days=7), hour, minute, tz)
    return start


def offset_text(dt):
    off = dt.utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    return "UTC%s%02d:%02d" % (sign, abs(mins) // 60, abs(mins) % 60)


def zone_text(settings, now):
    """How the zone is named in the report: the label, with the current offset when it is the machine's local zone."""
    if settings["tz"] is None:
        return "%s, %s" % (settings["tz_label"], offset_text(now.astimezone()))
    return settings["tz_label"]


# ---------------------------------------------------------------- formatting

def fmt(n):
    n = float(n)
    if abs(n) >= 1e9:
        return "%.2fB" % (n / 1e9)
    if abs(n) >= 1e6:
        return "%.2fM" % (n / 1e6)
    if abs(n) >= 1e3:
        return "%.1fK" % (n / 1e3)
    return "%d" % n


def pct(part, whole):
    return "%.1f%%" % (100.0 * part / whole) if whole else "0.0%"


def table(headers, rows, right=()):
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))

    def line(cells):
        out = []
        for i, c in enumerate(cells):
            s = str(c)
            out.append(s.rjust(widths[i]) if i in right else s.ljust(widths[i]))
        return "  ".join(out).rstrip()
    lines = [line(headers), "  ".join("-" * w for w in widths)]
    lines += [line(r) for r in rows]
    return "\n".join(lines)


# ---------------------------------------------------------------- reading the logs

def iter_lines(fh, cap=None):
    """Binary lines of an open file; a line longer than the cap is skipped, never loaded whole."""
    cap = MAX_LINE if cap is None else cap
    while True:
        raw = fh.readline(cap + 1)
        if not raw:
            return
        if len(raw) > cap and not raw.endswith(b"\n"):
            while True:
                rest = fh.readline(1 << 20)
                if not rest or rest.endswith(b"\n"):
                    break
            continue
        yield raw


def find_logs(root):
    """Every *.jsonl under root (symlinked folders are not followed), sorted."""
    found = []
    for base, dirs, files in os.walk(str(root)):
        dirs.sort()
        for name in files:
            if name.endswith(".jsonl"):
                found.append(os.path.join(base, name))
    return sorted(found)


def scan(root, scan_start):
    """-> (best, files_read, files_total, dup_ids). best maps message id -> row for assistant messages at or after scan_start."""
    cutoff = scan_start.timestamp()
    files = find_logs(root)
    best = {}                       # message id -> row (dedupe across files too)
    seen_files = defaultdict(set)
    files_read = 0
    for path in files:
        try:
            if os.path.getmtime(path) < cutoff:
                continue            # untouched since before the window: no in-window records
            fh = open(path, "rb")
        except OSError:
            continue
        files_read += 1
        per = {}
        with fh:
            for raw in iter_lines(fh):
                if b'"assistant"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue        # malformed line: skip
                if not isinstance(d, dict) or d.get("type") != "assistant":
                    continue
                m = d.get("message")
                if not isinstance(m, dict):
                    continue
                mid = m.get("id") or d.get("uuid")
                if not mid:
                    continue
                ts = parse_ts(d.get("timestamp"))
                e = per.get(mid)
                if e is None:
                    e = per[mid] = {"ts": ts, "model": m.get("model") or "?", "vals": None, "tools": {}}
                elif ts and (e["ts"] is None or ts < e["ts"]):
                    e["ts"] = ts
                u = m.get("usage")
                if isinstance(u, dict):
                    vals = tuple(to_int(u.get(k)) for k in FIELDS)
                    if e["vals"] is None or sum(vals) > sum(e["vals"]):
                        e["vals"] = vals
                content = m.get("content")
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            e["tools"][c.get("id") or len(e["tools"])] = c.get("name") or "?"
        rel = os.path.relpath(path, str(root))
        for mid, e in per.items():
            if e["ts"] is None or e["vals"] is None or e["ts"] < scan_start:
                continue
            seen_files[mid].add(path)
            row = {"file": rel, "model": e["model"], "ts": e["ts"], "vals": e["vals"], "tools": list(e["tools"].values())}
            old = best.get(mid)
            if old is None or sum(row["vals"]) > sum(old["vals"]):
                best[mid] = row
    dup_ids = sum(1 for s in seen_files.values() if len(s) > 1)
    return best, files_read, len(files), dup_ids


def summarize(entries):
    """entries: rows already filtered to a window."""
    tot = [0, 0, 0, 0]
    by_model = defaultdict(lambda: [0, 0, 0, 0])
    sessions = {}
    tools_calls = Counter()
    tools_est = defaultdict(float)
    tool_turn_tokens = 0
    n = 0
    for r in entries:
        n += 1
        v = r["vals"]
        t = sum(v)
        for i in range(4):
            tot[i] += v[i]
            by_model[r["model"]][i] += v[i]
        s = sessions.get(r["file"])
        if s is None:
            s = sessions[r["file"]] = {"vals": [0, 0, 0, 0], "models": Counter(), "first": r["ts"], "n": 0}
        for i in range(4):
            s["vals"][i] += v[i]
        s["models"][r["model"]] += t
        s["n"] += 1
        if r["ts"] < s["first"]:
            s["first"] = r["ts"]
        if r["tools"]:
            k = len(r["tools"])
            tool_turn_tokens += t
            for name in r["tools"]:
                tools_calls[name] += 1
                tools_est[name] += t / k
    return {"vals": tot, "models": dict(by_model), "sessions": sessions, "n": n,
            "tool_calls": tools_calls, "tool_est": tools_est, "tool_turn_tokens": tool_turn_tokens}


def in_window(entries, start, end=None):
    for r in entries:
        if r["ts"] >= start and (end is None or r["ts"] < end):
            yield r


def session_name(rel):
    parts = rel.replace("\\", "/").split("/")
    stem = os.path.splitext(parts[-1])[0]
    project = parts[0] if len(parts) > 1 else ""
    if "subagents" in parts:
        return "sub " + stem.replace("agent-", "")[:8], project, True
    return stem[:8], project, False


def model_rows(sm):
    rows = []
    for model in sorted(sm["models"], key=lambda m: -sum(sm["models"][m])):
        v = sm["models"][model]
        rows.append([model] + [fmt(x) for x in v] + [fmt(sum(v))])
    v = sm["vals"]
    rows.append(["TOTAL"] + [fmt(x) for x in v] + [fmt(sum(v))])
    return rows


def window_for(args_days, now, settings):
    """-> (window start, label, note). note is a sentence when the weekly window could not be used, else ''."""
    tz = settings["tz"]
    zt = zone_text(settings, now)
    if args_days is not None:
        start = now - timedelta(days=args_days)
        return start, "last %g days (from %s %s)" % (args_days, start.astimezone(tz).strftime("%Y-%m-%d %H:%M"), zt), ""
    if settings["reset"] is None:
        start = now - timedelta(days=7)
        note = ("no weekly reset is configured, so this shows a rolling 7 days; pass --reset-day and --reset-hour "
                "or fill logs/%s to match your plan's weekly window" % CONFIG_NAME)
        return start, "last 7 days, rolling (from %s %s)" % (start.astimezone(tz).strftime("%Y-%m-%d %H:%M"), zt), note
    day, hour, minute = settings["reset"]
    start = weekly_start(now, tz, day, hour, minute)
    label = "since last weekly reset, %s %02d:%02d (from %s %s)" % (DAY_NAMES[day].capitalize(), hour, minute,
                                                                 start.strftime("%Y-%m-%d %H:%M"), zt)
    return start, label, ""


def build_parser():
    ap = argparse.ArgumentParser(description="Claude Code token usage report (read-only).")
    ap.add_argument("--days", type=float, default=None, help="window = last N days instead of the weekly window")
    ap.add_argument("--top", type=int, default=10, help="top N sessions and tools (default 10)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--root", default=None, help="projects folder (default: <claude config folder>/projects)")
    ap.add_argument("--tz", default=None, help="time zone: IANA name, offset such as +02:00, UTC or local")
    ap.add_argument("--reset-day", default=None, help="weekday of your plan's weekly reset (monday..sunday or 0..6, 0 = Monday)")
    ap.add_argument("--reset-hour", default=None, help="hour of the weekly reset, 0..23 in that zone")
    ap.add_argument("--reset-minute", default=None, help="minute of the weekly reset (default 0)")
    ap.add_argument("--config", default=None, help="settings file (default: logs/%s)" % CONFIG_NAME)
    return ap


def main(argv=None, now=None):
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0) if isinstance(e.code, int) else 2
    if args.days is not None and not args.days > 0:
        print("--days must be more than 0", file=sys.stderr)
        return 2
    if args.top < 1:
        print("--top must be at least 1", file=sys.stderr)
        return 2
    try:
        settings = resolve_settings(args.tz, args.reset_day, args.reset_hour, args.reset_minute, load_config(args.config))
    except ConfigError as e:
        print("settings problem: %s" % e, file=sys.stderr)
        return 2

    t0 = now or datetime.now(timezone.utc)
    started = time.time()
    tz = settings["tz"]
    now_z = t0.astimezone(tz)
    zt = zone_text(settings, t0)
    today_start = at_wall(now_z.date(), 0, 0, tz)
    win_start, win_label, note = window_for(args.days, t0, settings)
    scan_start = min(win_start, today_start)

    root = args.root or str(default_projects_dir())
    if not os.path.isdir(root):
        print("No projects folder found at %s" % root, file=sys.stderr)
        return 1

    best, files_read, files_total, dup_ids = scan(root, scan_start)
    rows_all = list(best.values())
    W = summarize(in_window(rows_all, win_start))
    T = summarize(in_window(rows_all, today_start))

    # Consistency checks: per-session and per-model sums must equal the window total.
    sess_sum = sum(sum(s["vals"]) for s in W["sessions"].values())
    model_sum = sum(sum(v) for v in W["models"].values())
    win_total = sum(W["vals"])
    consistent = (sess_sum == win_total == model_sum)

    sess_list = []
    for rel, s in W["sessions"].items():
        name, project, is_sub = session_name(rel)
        model = s["models"].most_common(1)[0][0] if s["models"] else "?"
        tot = sum(s["vals"])
        sess_list.append({
            "session": name, "project": project, "subagent": is_sub,
            "date": s["first"].astimezone(tz).strftime("%Y-%m-%d"), "model": model,
            "total": tot, "cache_read": s["vals"][2], "cache_write": s["vals"][1],
            "fresh": s["vals"][0], "output": s["vals"][3], "messages": s["n"],
        })
    sess_list.sort(key=lambda x: -x["total"])
    top_sessions = sess_list[:args.top]

    tool_rows = [{"tool": name, "calls": W["tool_calls"][name], "est_tokens": W["tool_est"][name]}
                 for name in set(W["tool_calls"]) | set(W["tool_est"])]
    by_calls = sorted(tool_rows, key=lambda x: -x["calls"])[:args.top]
    by_est = sorted(tool_rows, key=lambda x: -x["est_tokens"])[:args.top]

    obs = []
    if note:
        obs.append(note[0].upper() + note[1:] + ".")
    if win_total:
        v = W["vals"]
        obs.append("cache reads are %s of window tokens; cache writes %s, fresh input %s, output %s."
                   % (pct(v[2], win_total), pct(v[1], win_total), pct(v[0], win_total), pct(v[3], win_total)))
        if v[3]:
            obs.append("cache reads are %.0fx the output tokens (%s vs %s)." % (v[2] / v[3], fmt(v[2]), fmt(v[3])))
    if sess_list:
        top = sess_list[0]
        obs.append("largest session: %s (%s, %s, %s) = %s of window tokens."
                   % (top["session"], top["project"], top["date"], top["model"], pct(top["total"], win_total)))
        top3 = sum(x["total"] for x in sess_list[:3])
        obs.append("top 3 sessions = %s of window tokens; %d sessions in total." % (pct(top3, win_total), len(sess_list)))
        sub_total = sum(x["total"] for x in sess_list if x["subagent"])
        obs.append("subagent logs account for %s of window tokens." % pct(sub_total, win_total))
    if W["models"]:
        mt = max(W["models"], key=lambda m: sum(W["models"][m]))
        obs.append("biggest model: %s with %s of window tokens." % (mt, pct(sum(W["models"][mt]), win_total)))
    if win_total:
        obs.append("turns with tool calls carry %s of window tokens; turns without tools %s."
                   % (pct(W["tool_turn_tokens"], win_total), pct(win_total - W["tool_turn_tokens"], win_total)))
    if win_total and sum(T["vals"]):
        obs.append("today is %s of the window total (%s tokens today)." % (pct(sum(T["vals"]), win_total), fmt(sum(T["vals"]))))
    if dup_ids:
        obs.append("%d message ids appeared in more than one log file; each counted once." % dup_ids)
    if not consistent:
        obs.append("WARNING: per-session or per-model sums do not match the window total.")

    result = {
        "generated_utc": t0.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "timezone": zt,
        "weekly_reset": None if settings["reset"] is None else {"day": DAY_NAMES[settings["reset"][0]], "hour": settings["reset"][1], "minute": settings["reset"][2]},
        "window": {"label": win_label, "start": win_start.astimezone(tz).isoformat(timespec="seconds"),
                   "messages": W["n"], "fresh": W["vals"][0], "cache_write": W["vals"][1],
                   "cache_read": W["vals"][2], "output": W["vals"][3], "total": win_total,
                   "models": {m: dict(zip(COLS, v)) for m, v in W["models"].items()}},
        "today": {"start": today_start.isoformat(timespec="seconds"), "messages": T["n"],
                  "fresh": T["vals"][0], "cache_write": T["vals"][1], "cache_read": T["vals"][2],
                  "output": T["vals"][3], "total": sum(T["vals"]),
                  "models": {m: dict(zip(COLS, v)) for m, v in T["models"].items()}},
        "sessions": top_sessions,
        "tools_by_calls": by_calls,
        "tools_by_est_tokens": by_est,
        "observations": obs,
        "checks": {"sessions_sum": sess_sum, "models_sum": model_sum, "window_total": win_total, "consistent": consistent},
        "files": {"found": files_total, "read": files_read},
    }

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if consistent else 2

    out = []
    out.append("Claude Code token usage (read-only). Generated %s (%s), %.1fs." % (now_z.strftime("%Y-%m-%d %H:%M"), zt, time.time() - started))
    out.append("Logs: %d files found, %d touched in range. Window: %s." % (files_total, files_read, win_label))
    out.append("")
    hdr = ["model", "fresh input", "cache write", "cache read", "output", "total"]
    out.append("1. TOTALS - window (%d messages)" % W["n"])
    out.append(table(hdr, model_rows(W), right=range(1, 6)))
    out.append("")
    out.append("   TODAY (since %s, %d messages)" % (today_start.strftime("%Y-%m-%d 00:00 ") + zt, T["n"]))
    out.append(table(hdr, model_rows(T), right=range(1, 6)))
    out.append("")
    out.append("2. TOP %d SESSIONS in window (by total tokens)" % args.top)
    srows = []
    for i, x in enumerate(top_sessions, 1):
        tot = x["total"]
        srows.append([i, x["session"], x["project"][:34], x["date"], x["model"], fmt(tot),
                      pct(x["cache_read"], tot), pct(x["cache_write"], tot), pct(x["fresh"], tot), pct(x["output"], tot)])
    out.append(table(["#", "session", "project", "date", "model", "total", "cache rd", "cache wr", "fresh", "output"],
                     srows, right=(0, 5, 6, 7, 8, 9)))
    out.append("")
    out.append("3. TOOLS (estimate: each turn's tokens split evenly across its tool_use blocks)")
    out.append("   by call count:")
    out.append(table(["tool", "calls", "est. tokens"], [[t["tool"], t["calls"], fmt(t["est_tokens"])] for t in by_calls], right=(1, 2)))
    out.append("   by estimated tokens:")
    out.append(table(["tool", "calls", "est. tokens"], [[t["tool"], t["calls"], fmt(t["est_tokens"])] for t in by_est], right=(1, 2)))
    out.append("")
    out.append("4. OBSERVATIONS")
    for o in obs:
        out.append("   - " + o)
    out.append("")
    out.append("Checks: sessions sum %s, models sum %s, window total %s -> %s."
               % (fmt(sess_sum), fmt(model_sum), fmt(win_total), "OK" if consistent else "MISMATCH"))
    print("\n".join(out))
    return 0 if consistent else 2


if __name__ == "__main__":
    sys.exit(main())
