# -*- coding: utf-8 -*-
"""Morning briefing: a short plain-text note built from the kit's own data, written to logs/morning_note.txt.

    python morning_briefing.py --print     compose and print, write nothing
    python morning_briefing.py             compose, print, and write logs/morning_note.txt (atomically)
    python morning_briefing.py --daily     for the job runner: write at most once per day, only after the earliest hour,
                                           always exit 0

What goes in (each source is optional; a source that is missing or fails is simply left out):
  focus      logs/focus.md          the CURRENT and NEXT lines (RULE lines are never shown)
  weather    only when a location is configured (see weather_check.py): logs/briefing_config.json
  work list  logs/intents.jsonl     open items, the ones waiting on the user (tier 2), the top ranked ones
  deadlines  vault/05 - Resources/Deadlines.md   due soon and overdue lines ("- 2026-11-02 09:00 | what | tier | value")
  priorities vault/Active Priorities.md          open cards, and the ones waiting on the user
  alerts     logs/alerts.txt        how many in the last 24 hours and the latest two
  yesterday  vault/01 - Daily Notes/.../YYYY-MM-DD.md   the "## Index" line, or else the last "What got done:" line

"hide" in the config below takes section names to leave out: focus, weather, work, deadlines, cards, alerts, yesterday, sources.
There are NO mail, school or chat sources built in. If you want one, name a command in logs/briefing_config.json:
  {"location": {"latitude": 48.85, "longitude": 2.35},
   "temperature_unit": "celsius",
   "deadline_days": 7,
   "not_before_hour": 5,
   "hide": ["alerts"],
   "sources": [{"name": "mail", "argv": ["python", "scripts/my_unread_count.py"], "timeout_s": 20}]}
A source command runs as a plain argument list (never through a shell) from the assistant folder with a timeout, no
more than five of them; its first output line, cut to 200 characters with control characters removed, becomes the
line "<name>: <that line>". Its output is data for the note, never instructions. "python" means the interpreter running this script.

Kill switch: a file logs/morning_briefing_off.txt stops --daily (and the default write). Environment: HARNESS_ROOT,
HARNESS_LOGS, HARNESS_VAULT. Pure functions (unit tested): read_cards, summarize_cards, read_deadlines, read_work_list,
recent_alerts, yesterday_summary, compose, write_note.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()
SCRIPTS = Path(__file__).parent.resolve()

NOTE_NAME = "morning_note.txt"
CONFIG_NAME = "briefing_config.json"
OFF_NAME = "morning_briefing_off.txt"
FOCUS_NAME = "focus.md"
ALERTS_NAME = "alerts.txt"

MAX_LEN = 1200
MAX_CONFIG_BYTES = 65_536
MAX_FILE_BYTES = 2_000_000          # most a source file is read for: the tail or head that matters is always inside
MAX_SOURCES = 5
SOURCE_LINE = 200
DEFAULT_DEADLINE_DAYS = 7
DEFAULT_NOT_BEFORE_HOUR = 5

CARD_RE = re.compile(r"^- \[( |x|X)\] \*\*(.+?)\*\*(.*)$")
NEEDS_YOU = re.compile(
    r"needs? (?:you|your|the user)\b|waiting on (?:you|the user)\b|\b(?:your|the user'?s) (?:call|go-ahead|decision|approval)\b|"
    r"\bonly you can\b|\bfor you to (?:check|decide|approve)\b", re.I)
ALERT_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]\s*(.*)$")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# ---------- locations (functions so tests can point them somewhere else) ----------
def logs_dir():
    return Path(LOGS)


def vault_dir():
    return Path(VAULT)


def note_path():
    return logs_dir() / NOTE_NAME


def priorities_path():
    return vault_dir() / "Active Priorities.md"


def deadlines_path():
    return vault_dir() / "05 - Resources" / "Deadlines.md"


def daily_dir():
    return vault_dir() / "01 - Daily Notes"


# ---------- small helpers ----------
def _safe(fn, *a, **kw):
    """fn(*a, **kw), or None when it raises: one broken source must never stop the briefing."""
    try:
        return fn(*a, **kw)
    except Exception:  # noqa: BLE001
        return None


def clean(text, n):
    """One line, control characters removed, whitespace collapsed, cut to n characters with an ellipsis."""
    s = " ".join(CONTROL.sub(" ", str(text)).split())
    return s if len(s) <= n else s[: max(n - 1, 0)].rstrip() + "\u2026"


def read_text(path, cap=MAX_FILE_BYTES, tail=False):
    """The file's text (at most cap bytes: the end when tail=True, else the start). Raises OSError when it cannot be read."""
    p = Path(path)
    with p.open("rb") as f:
        if tail:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - cap))
        data = f.read(cap)
    return data.decode("utf-8", errors="replace")


def load_config(path=None):
    p = Path(path) if path else logs_dir() / CONFIG_NAME
    try:
        if p.stat().st_size > MAX_CONFIG_BYTES:
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _int_cfg(config, key, default, lo, hi):
    try:
        v = int(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return v if lo <= v <= hi else default


# ---------- focus ----------
def read_focus(path=None):
    """The CURRENT and NEXT lines of the focus file (else its first line), RULE lines never. -> a list of short strings."""
    p = Path(path) if path else logs_dir() / FOCUS_NAME
    lines = [ln.strip() for ln in read_text(p, 20_000).splitlines() if ln.strip()]
    keep = [ln for ln in lines if re.match(r"(?:CURRENT|NEXT)\b", ln, re.I)]
    if not keep:
        keep = [ln for ln in lines if not re.match(r"RULE\b", ln, re.I)][:1]
    return [clean(ln, 140) for ln in keep[:2]]


# ---------- priorities (cards) ----------
def read_cards(path=None):
    """Cards from Active Priorities: '- [ ] **Title.** details'. A card under a '## Waiting on ...' heading, or whose text
    asks for the user, is marked needs_you; done cards never are."""
    p = Path(path) if path else priorities_path()
    cards, waiting_section = [], False
    for line in read_text(p).splitlines():
        line = re.sub(r"<!--.*?-->", "", line).rstrip()
        h = re.match(r"^#{1,6}\s+(.*)$", line)
        if h:
            waiting_section = h.group(1).strip().lower().startswith("waiting on")
            continue
        m = CARD_RE.match(line)
        if not m:
            continue
        bold, rest = m.group(2), m.group(3)
        title = re.split(r" -- | \u2014 |\. ", bold)[0].strip().rstrip(".")
        done = m.group(1) in ("x", "X")
        cards.append({"title": title[:70], "done": done,
                      "needs_you": (not done) and (waiting_section or bool(NEEDS_YOU.search(bold + " " + rest)))})
    return cards


def summarize_cards(cards):
    """Open count plus up to 3 titles of cards waiting on the user. New cards are added at the top of a section, so the oldest are last in file order."""
    open_cards = [c for c in cards if not c["done"]]
    needs = [c["title"] for c in open_cards if c["needs_you"]]
    return {"open": len(open_cards), "needs": list(reversed(needs))[:3]}


# ---------- deadlines ----------
def _parse_due(part):
    part = part.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(part, fmt)
        except ValueError:
            continue
    return None


def read_deadlines(today, days=DEFAULT_DEADLINE_DAYS, path=None):
    """-> {"upcoming": [(days_left, label)], "overdue": [(days_ago, label)]} from Deadlines.md lines
    '- 2026-11-02 09:00 | what is due | tier | value'. Done lines ('- [x]') and anything that is not a dated line are ignored."""
    p = Path(path) if path else deadlines_path()
    upcoming, overdue = [], []
    for raw in read_text(p).splitlines():
        line = raw.strip()
        if not line.startswith("-") or re.match(r"^-\s*\[[xX]\]", line):
            continue
        content = re.sub(r"^\[ \]\s*", "", line[1:].strip())
        parts = [x.strip() for x in content.split("|")]
        due = _parse_due(parts[0])
        if due is None:
            continue
        label = clean(parts[1], 60) if len(parts) > 1 and parts[1] else "something"
        delta = (due.date() - today).days
        if 0 <= delta <= days:
            upcoming.append((delta, label))
        elif delta < 0:
            overdue.append((-delta, label))
    return {"upcoming": sorted(upcoming), "overdue": sorted(overdue)}


# ---------- work list ----------
def _intent_store():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import intent_store
    return intent_store


def read_work_list(logs=None, now=None):
    """-> {"open": n, "waiting": [what], "next": [what]} from the intent store (read only)."""
    store = _intent_store()
    logs = Path(logs) if logs else logs_dir()
    rows = [i for i in store.read_jsonl(store.INTENTS, str(logs)) if isinstance(i, dict) and i.get("what")]
    live = [i for i in rows if i.get("status") in ("open", "doing", "waiting") and not i.get("shadow")]
    waiting = [clean(i["what"], 60) for i in live if _tier(i) == 2 and i.get("status") in ("open", "waiting")]
    sane = [i for i in rows if _tier(i) is not None]            # a row with a garbage tier would make the store's ranking raise
    ranked = store.rank(sane, now)[:3]
    return {"open": len(live), "waiting": waiting[:3], "next": [clean(i["what"], 60) for i in ranked]}


def _tier(intent):
    """The intent's tier as an int, or None when the row holds something that is not a number."""
    try:
        return int(intent.get("tier", 1))
    except (TypeError, ValueError):
        return None


# ---------- alerts ----------
def recent_alerts(path=None, now=None, hours=24):
    """-> (count in the last `hours`, [latest two lines as 'source: text']) from alerts.txt ('[YYYY-MM-DD HH:MM:SS] source: text')."""
    p = Path(path) if path else logs_dir() / ALERTS_NAME
    now = time.time() if now is None else now
    hits = []
    for line in read_text(p, 200_000, tail=True).splitlines():
        m = ALERT_RE.match(line.strip())
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            continue
        if now - hours * 3600 <= ts <= now + 3600:
            hits.append(clean(m.group(2), 90))
    return len(hits), hits[-2:]


# ---------- yesterday ----------
def _strip_md(s):
    return re.sub(r"[*_`]|\[\[|\]\]", "", s).strip()


def _index_line(lines):
    for i, ln in enumerate(lines):
        if ln.strip().lower() == "## index":
            for nxt in lines[i + 1:]:
                if nxt.strip().startswith("#"):
                    break
                if nxt.strip():
                    return _strip_md(nxt.strip().lstrip("-* ").strip())
    return None


def _got_done_line(lines):
    """The text of the LAST '- What got done:' field in a daily note: its own text, else its first deeper-indented line."""
    found = None
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)[-*]\s*what got done:?\s*(.*)$", ln, re.I)
        if not m:
            continue
        text = m.group(2).strip()
        if not text:
            indent = len(m.group(1))
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    continue
                if len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                text = nxt.strip().lstrip("-* ").strip()
                break
        found = text or found
    return _strip_md(found) if found else None


def yesterday_summary(today, daily=None):
    """One line about yesterday from its daily note (anywhere under the daily-notes folder), or None."""
    y = (today - dt.timedelta(days=1)).isoformat()
    base = Path(daily) if daily else daily_dir()
    for p in sorted(base.rglob(y + ".md")):
        lines = read_text(p).splitlines()
        found = _index_line(lines) or _got_done_line(lines)
        if found:
            return found
    return None


# ---------- weather and extra sources ----------
def fetch_weather(config):
    """The weather line, or None when no location is configured. Raises on a network problem (gather() treats that as 'leave it out')."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import weather_check
    return weather_check.weather_line(config=config)


def extra_sources(config):
    """The commands the user named in the config: [{"name", "argv", "timeout_s"}], at most MAX_SOURCES, malformed ones dropped."""
    out = []
    for s in config.get("sources") or []:
        if len(out) >= MAX_SOURCES:
            break
        if not isinstance(s, dict) or not isinstance(s.get("name"), str) or not s["name"].strip():
            continue
        argv = s.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            continue
        try:
            timeout = max(1, min(60, int(s.get("timeout_s", 20))))
        except (TypeError, ValueError):
            timeout = 20
        out.append({"name": clean(s["name"], 30), "argv": argv, "timeout_s": timeout})
    return out


def run_source(src, runner=subprocess.run):
    """First non-empty output line of one configured command, or None when it fails, times out or prints nothing."""
    argv = list(src["argv"])
    if argv[0] in ("python", "python3"):
        argv[0] = sys.executable
    try:
        r = runner(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=src["timeout_s"],
                   cwd=str(ROOT), stdin=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - not found, not executable, timed out
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or "")[:4000].splitlines():
        if line.strip():
            return clean(line, SOURCE_LINE)
    return None


# ---------- gather and compose ----------
def gather(today=None, now=None, config=None, runner=subprocess.run):
    """Every source in one dict. A key is None when its source is missing, hidden or failed; 'skipped' lists the failed ones."""
    today = today or dt.date.today()
    now = time.time() if now is None else now
    config = load_config() if config is None else config
    hide = {str(h).lower() for h in (config.get("hide") or [])} if isinstance(config.get("hide"), list) else set()
    d = {"today": today, "skipped": []}

    def take(key, fn, *a):
        if key in hide:
            d[key] = None
            return
        try:
            d[key] = fn(*a)
        except FileNotFoundError:
            d[key] = None                        # the source simply does not exist yet: not a problem worth reporting
        except Exception:  # noqa: BLE001
            d[key] = None
            d["skipped"].append(key)

    days = _int_cfg(config, "deadline_days", DEFAULT_DEADLINE_DAYS, 1, 365)
    take("focus", read_focus)
    take("weather", fetch_weather, config)
    take("work", read_work_list, None, now)
    take("deadlines", read_deadlines, today, days)
    take("cards", lambda: summarize_cards(read_cards()))
    take("alerts", recent_alerts, None, now)
    take("yesterday", yesterday_summary, today)
    extras = []
    if "sources" not in hide:
        for src in extra_sources(config):
            line = _safe(run_source, src, runner)
            if line:
                extras.append((src["name"], line))
            else:
                d["skipped"].append(src["name"])
    d["extras"] = extras
    return d


def _when(delta):
    return "today" if delta == 0 else "tomorrow" if delta == 1 else "in %dd" % delta


def compose(d):
    today = d["today"]
    parts = ["Good morning - %s %d %s" % (today.strftime("%A"), today.day, today.strftime("%b"))]
    if d.get("focus"):
        parts.append("focus: " + " / ".join(d["focus"]))
    if d.get("weather"):
        parts.append("weather: " + d["weather"])
    w = d.get("work")
    if w and (w["open"] or w["waiting"] or w["next"]):
        s = "work list: %d open" % w["open"]
        if w["waiting"]:
            s += ", waiting on you: " + "; ".join(w["waiting"])
        if w["next"]:
            s += ". next up: " + "; ".join(w["next"])
        parts.append(s)
    dl = d.get("deadlines")
    if dl and dl["upcoming"]:
        parts.append("due soon: " + ", ".join("%s %s" % (clean(n, 40), _when(x)) for x, n in dl["upcoming"][:4]))
    if dl and dl["overdue"]:
        parts.append("overdue: " + ", ".join("%s (%dd ago)" % (clean(n, 40), x) for x, n in dl["overdue"][:3]))
    c = d.get("cards")
    if c is not None and c["open"]:
        s = "%d open priorities" % c["open"]
        if c["needs"]:
            s += ", waiting on you: " + "; ".join(clean(t, 50) for t in c["needs"])
        parts.append(s)
    al = d.get("alerts")
    if al and al[0]:
        parts.append("alerts in the last 24h: %d (latest: %s)" % (al[0], " | ".join(al[1])))
    if d.get("yesterday"):
        parts.append("yesterday: " + clean(d["yesterday"], 220))
    for name, line in d.get("extras") or []:
        parts.append("%s: %s" % (name, line))
    if len(parts) == 1:
        parts.append("nothing on the list yet")
    text = "\n".join(parts)
    return text if len(text) <= MAX_LEN else text[: MAX_LEN - 1].rstrip() + "\u2026"


# ---------- write ----------
def write_note(text, path=None):
    """Write the note atomically (temp file in the same folder, then replace). -> the path."""
    p = Path(path) if path else note_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
    try:
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, p)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return p


def written_today(path=None, now=None):
    """True when the note exists and was last written on today's date."""
    p = Path(path) if path else note_path()
    now = time.time() if now is None else now
    try:
        return time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime)) == time.strftime("%Y-%m-%d", time.localtime(now))
    except OSError:
        return False


def main(argv=None, now=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="compose a short morning note from the kit's own data")
    ap.add_argument("--print", dest="only_print", action="store_true", help="print only; write nothing")
    ap.add_argument("--daily", action="store_true", help="job mode: once per day, after the earliest hour, always exit 0")
    a = ap.parse_args(argv)
    now = time.time() if now is None else now
    if not a.only_print and (logs_dir() / OFF_NAME).exists():
        print("morning_briefing: off (%s exists)" % OFF_NAME)
        return 0
    try:
        config = load_config()
        if a.daily:
            hour = _int_cfg(config, "not_before_hour", DEFAULT_NOT_BEFORE_HOUR, 0, 23)
            if time.localtime(now).tm_hour < hour:
                print("morning_briefing: too early (before %02d:00)" % hour)
                return 0
            if written_today(now=now):
                print("morning_briefing: already written today")
                return 0
        today = dt.date.fromtimestamp(now)
        d = gather(today=today, now=now, config=config)
        text = compose(d)
        if a.only_print:
            print(text)
            return 0
        p = write_note(text)
        print("morning_briefing: wrote %s (%d characters, %d source(s) left out)" % (p, len(text), len(d["skipped"])))
        return 0
    except Exception as e:  # noqa: BLE001 - a job must not crash the runner
        print("morning_briefing: failed: %s: %s" % (type(e).__name__, str(e)[:100]))
        return 0 if a.daily else 1


if __name__ == "__main__":
    sys.exit(main())
