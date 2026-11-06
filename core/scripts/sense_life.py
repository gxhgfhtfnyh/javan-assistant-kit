# -*- coding: utf-8 -*-
"""Life sensor (OPTIONAL, off until you point it at a calendar): turns what is coming up in the user's calendar into work items with a sensible lead time, so the
assistant acts without being told. A birthday that sits in the calendar should not need a reminder from the user.
  - the user's own birthday or anniversary: the day itself, from 06:00 (a friend wishes you in the morning)                  tier 1, value 5
  - someone else's birthday or an anniversary: a heads-up the evening before (18:00) and again that morning (08:00); wishing them is the user's call   tier 1, value 4
  - a timed event (not all-day) starting within 3 hours: one reminder                                                         tier 1, value 4
  - other all-day entries are left alone (holiday markers and the like).
Past occasions are not replayed (once the day is over it is dropped). It perceives only: it messages nobody and writes nothing to any calendar.

Where the events come from: a plain iCalendar (.ics) file or link you supply, nothing provider specific. Export your calendar to a .ics file, or copy the private
"secret address in iCal format" most calendar apps offer, and put ONE of these in logs/life_config.json:
    {"ics_path": "/full/path/to/calendar.ics", "owner_name": "Sam"}          (a relative path is read from the assistant folder)
    {"ics_url": "https://example.com/private/calendar.ics", "owner_name": "Sam"}
owner_name is optional: an entry is the user's own when it starts with "my"/"our" ("My bday") or contains that name. A secret calendar link is a password:
logs/life_config.json then holds a secret, so keep it out of every backup or repository you share and put it on the leak scan's ignore list; the link is never
printed or put into an alert. Repeating events (yearly birthdays, weekly meetings) are expanded here for the next 3 days; daily, weekly (BYDAY), monthly and yearly
repeats with INTERVAL, COUNT, UNTIL and EXDATE are understood, anything fancier counts as a single event. Calendar text is untrusted DATA: titles are cut to 80
characters, control characters are stripped and nothing in a title is ever acted on. At most 5 MB is read.
When logs/life_config.json is missing this prints one 'set up sense_life: ...' line and exits 0.

A downloaded calendar is cached for 6 hours (logs/life_cache.json) so a 15-minute tick does not fetch every time; a local file is re-read each run. A failed refresh keeps
the old cache and never raises; it is reported once every 6 hours through notify.alert.
    python sense_life.py [--dry-run]
Kill switch: logs/sense_life_off.txt (a dry run ignores it). Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): kind_of(summary, owner_name), due(events, now, owner_name), parse_ics(text, lo, hi), expand(start, rule, lo, hi)."""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

OFF = "sense_life_off.txt"
CACHE = "life_cache.json"
CONFIG = "life_config.json"
STATE = "sense_life_state.json"
CACHE_SEC = 6 * 3600
ALERT_EVERY_SEC = 6 * 3600
LOOKBACK_SEC = 86400
LOOKAHEAD_SEC = 3 * 86400
MAX_ICS_BYTES = 5 * 1024 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_VEVENTS = 5000
MAX_CACHED = 500
MAX_STEPS = 20000
SUMMARY_MAX = 80
OCCASION = re.compile(r"\b(birthday|b'?day|bday|anniv\w*)\b", re.I)
OWN_LEAD = re.compile(r"^\s*(my|our)\b", re.I)
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
SUPPORTED_KEYS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST", "BYMONTH", "BYMONTHDAY"}
TZID_OK = re.compile(r"[A-Za-z0-9_+\-/]{1,64}")


def logs_dir():
    return Path(LOGS)


def _clean(text, limit=SUMMARY_MAX):
    """Pure. Untrusted text made safe to quote: control characters become spaces, double quotes become single, whitespace collapses, cut to `limit`."""
    s = "".join(ch if ch.isprintable() else " " for ch in str(text if text is not None else ""))
    return " ".join(s.replace('"', "'").split())[:limit].strip()


# ---------- classification and timing (pure) ----------
def kind_of(summary, owner_name=None):
    """Pure. own_occasion | friend_occasion | event. An occasion is the user's own when it starts with my/our or contains `owner_name`."""
    s = summary or ""
    if OCCASION.search(s):
        if OWN_LEAD.search(s):
            return "own_occasion"
        if owner_name and re.search(r"\b%s\b" % re.escape(str(owner_name).strip()), s, re.I):
            return "own_occasion"
        return "friend_occasion"
    return "event"


def _start_epoch(start, all_day):
    if all_day:
        return time.mktime(dt.datetime(start.year, start.month, start.day).timetuple())
    if start.tzinfo is not None:
        return start.timestamp()
    return time.mktime(start.timetuple())


def _at(day_epoch, hour, minute=0):
    d = dt.datetime.fromtimestamp(day_epoch)
    return time.mktime(d.replace(hour=hour, minute=minute, second=0, microsecond=0).timetuple())


def _slug(summary):
    s = re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:20]
    return "%s-%s" % (s or "x", hashlib.sha1(summary.encode("utf-8", "replace")).hexdigest()[:6])


def due(events, now, owner_name=None):
    """Pure. events: [{summary, start (date or datetime)}]. -> [(kind, what, tier, value, dedupe, not_before_epoch, deadline_epoch)]."""
    out = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        raw, start = (e.get("summary") or "").strip(), e.get("start")
        summary = _clean(raw)
        if not summary or not isinstance(start, dt.date):
            continue
        all_day = not isinstance(start, dt.datetime)
        t0 = _start_epoch(start, all_day)
        day = dt.date.fromtimestamp(t0).isoformat()
        kind = kind_of(raw, owner_name)
        slug = _slug(raw)
        if kind != "event" and all_day:
            if now >= t0 + 24 * 3600:
                continue                                                  # the day is over: a past occasion is not replayed
            if kind == "own_occasion":
                nb = _at(t0, 6)
                if now >= nb:
                    out.append((kind, "Today is the user's own occasion ('%s'). Wish them warmly in one short casual message, like a friend would; do not make it a report." % summary,
                                1, 5, "life-own-%s-%s" % (slug, day), nb, t0 + 24 * 3600))
            else:
                if _at(t0 - 86400, 18) <= now < t0:
                    out.append((kind, "Heads-up for tomorrow: '%s'. Tell the user in one casual line; wishing someone is the user's call." % summary,
                                1, 4, "life-eve-%s-%s" % (slug, day), _at(t0 - 86400, 18), t0))
                if _at(t0, 8) <= now < t0 + 24 * 3600:
                    out.append((kind, "Today: '%s'. Remind the user in one casual line; offer to draft a short wish for them to approve (nothing is sent without their say)." % summary,
                                1, 4, "life-day-%s-%s" % (slug, day), _at(t0, 8), t0 + 24 * 3600))
        elif not all_day and 0 <= t0 - now <= 3 * 3600:
            out.append((kind, "In %d min: '%s' (%s). Remind the user in one casual line." % ((t0 - now) // 60, summary, time.strftime("%H:%M", time.localtime(t0))),
                        1, 4, "life-evt-%s-%s" % (slug, time.strftime("%Y%m%d%H%M", time.localtime(t0))), now, t0))
    return out


# ---------- iCalendar reading (pure) ----------
def unfold(text):
    """Pure. The lines of an iCalendar text with folded continuation lines joined."""
    lines = []
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_outside_quotes(text, sep):
    parts, cur, quoted = [], [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        if ch == sep and not quoted:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def parse_content_line(line):
    """Pure. 'NAME;PARAM=V:value' -> (NAME, {PARAM: V}, value), or None for a line that is not a property."""
    quoted, idx = False, None
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            idx = i
            break
    if idx is None:
        return None
    parts = _split_outside_quotes(line[:idx], ";")
    name = parts[0].strip().upper()
    if not name:
        return None
    params = {}
    for p in parts[1:]:
        k, eq, v = p.partition("=")
        if eq:
            params[k.strip().upper()] = v.strip().strip('"')
    return name, params, line[idx + 1:]


def _unescape(value):
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


_ZONES = {}


def _zone(tzid):
    """A ZoneInfo for a TZID from the file, or None when it is odd or unknown (the time is then read as local wall time)."""
    if not TZID_OK.fullmatch(tzid or ""):
        return None
    if tzid not in _ZONES:
        try:
            import zoneinfo
            _ZONES[tzid] = zoneinfo.ZoneInfo(tzid)
        except Exception:                                           # noqa: BLE001 - no tz database on this system, or an unknown name
            _ZONES[tzid] = None
    return _ZONES[tzid]


def parse_dt(value, params=None):
    """Pure-ish. An iCalendar DATE or DATE-TIME value -> datetime.date or datetime.datetime (aware for Z and known TZID, naive local otherwise). Raises ValueError."""
    params = params or {}
    v = (value or "").strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", v):
        if not re.fullmatch(r"\d{8}", v):
            raise ValueError("bad date")
        return dt.date(int(v[0:4]), int(v[4:6]), int(v[6:8]))
    m = re.fullmatch(r"(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)(Z?)", v)
    if not m:
        raise ValueError("bad date-time")
    d = dt.datetime(*(int(x) for x in m.groups()[:6]))
    if m.group(7):
        return d.replace(tzinfo=dt.timezone.utc)
    z = _zone(params.get("TZID", "")) if params.get("TZID") else None
    return d.replace(tzinfo=z) if z else d


def _epoch_of(x):
    if isinstance(x, dt.datetime):
        return x.timestamp() if x.tzinfo is not None else time.mktime(x.timetuple())
    return time.mktime(dt.datetime(x.year, x.month, x.day).timetuple())


def _safe_epoch(x):
    """_epoch_of, or None for a date the system clock cannot express (Windows cannot convert dates before 1970; a 1965 birthday must not stop the whole read)."""
    try:
        return _epoch_of(x)
    except (OverflowError, OSError, ValueError):
        return None


def parse_rrule(text):
    """Pure. 'FREQ=YEARLY;INTERVAL=2' -> {'FREQ': 'YEARLY', 'INTERVAL': '2'} (keys upper-case)."""
    rule = {}
    for part in str(text or "").split(";"):
        k, eq, v = part.partition("=")
        if eq and k.strip():
            rule[k.strip().upper()] = v.strip()
    return rule


def rule_supported(rule, start):
    """Pure. True when expand() understands this rule for an event starting at `start`."""
    if rule.get("FREQ", "").upper() not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return False
    if set(rule) - SUPPORTED_KEYS:
        return False
    for key in ("INTERVAL", "COUNT"):
        if key in rule and not (rule[key].isdigit() and int(rule[key]) >= 1):
            return False
    freq = rule["FREQ"].upper()
    if "BYDAY" in rule and (freq != "WEEKLY" or not all(t in WEEKDAYS for t in rule["BYDAY"].upper().split(","))):
        return False
    if "BYMONTH" in rule and (freq != "YEARLY" or rule["BYMONTH"] != str(start.month)):
        return False
    if "BYMONTHDAY" in rule and (freq not in ("MONTHLY", "YEARLY") or rule["BYMONTHDAY"] != str(start.day)):
        return False
    return True


def _candidates(start, freq, interval, byday, p0):
    """Ascending candidate starts of a repeating event, from period number p0 on."""
    p = p0
    while True:
        if freq == "YEARLY":
            try:
                yield start.replace(year=start.year + p * interval)
            except ValueError:
                pass                                                 # 29 Feb in a year without one: skipped, as the standard says
        elif freq == "MONTHLY":
            m0 = start.month - 1 + p * interval
            try:
                yield start.replace(year=start.year + m0 // 12, month=m0 % 12 + 1)
            except ValueError:
                pass                                                 # day 31 in a short month: skipped
        elif freq == "WEEKLY":
            base = start + dt.timedelta(days=p * interval * 7)
            monday = base - dt.timedelta(days=base.weekday())
            for wd in sorted(byday):
                c = monday + dt.timedelta(days=wd)
                if c >= start:
                    yield c
        else:
            yield start + dt.timedelta(days=p * interval)
        p += 1


def _first_period(start, freq, interval, lo_ep):
    sd = start.date() if isinstance(start, dt.datetime) else start
    ld = dt.date.fromtimestamp(lo_ep)
    if freq == "YEARLY":
        n = (ld.year - sd.year) // interval
    elif freq == "MONTHLY":
        n = ((ld.year - sd.year) * 12 + ld.month - sd.month) // interval
    elif freq == "WEEKLY":
        n = (ld - sd).days // (7 * interval)
    else:
        n = (ld - sd).days // interval
    return max(0, n - 1)


def expand(start, rule, lo_ep, hi_ep):
    """Pure. Yield (occurrence, epoch) of a repeating event whose epoch is in [lo_ep, hi_ep). `rule` is a supported rule (see rule_supported). Bounded to 20000 steps."""
    freq = rule["FREQ"].upper()
    interval = int(rule.get("INTERVAL", 1))
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    until_ep = None
    if "UNTIL" in rule:
        try:
            until_ep = _epoch_of(parse_dt(rule["UNTIL"]))
        except ValueError:
            until_ep = None
    byday = [WEEKDAYS[t] for t in rule.get("BYDAY", "").upper().split(",") if t in WEEKDAYS] or [start.weekday()]
    p0 = 0 if count is not None else _first_period(start, freq, interval, lo_ep)
    seen = steps = 0
    try:
        for cand in _candidates(start, freq, interval, byday, p0):
            steps += 1
            if steps > MAX_STEPS:
                return
            if count is not None and seen >= count:
                return
            seen += 1
            ep = _safe_epoch(cand)
            if ep is None:
                if cand.year < 1980:
                    continue                                          # a long-past repeat the clock cannot express still counts towards COUNT
                return
            if until_ep is not None and ep > until_ep:
                return
            if ep >= hi_ep:
                return
            if ep >= lo_ep:
                yield cand, ep
    except OverflowError:
        return                                                        # ran past the last date the calendar can hold


def _vevents(text):
    """Pure. The VEVENT blocks of an iCalendar text as property dicts (alarms inside an event are skipped), at most MAX_VEVENTS."""
    blocks, cur, in_alarm = [], None, False
    for line in unfold(text):
        up = line.strip().upper()
        if up == "BEGIN:VEVENT":
            cur, in_alarm = {"EXDATE": []}, False
        elif up == "END:VEVENT":
            if cur is not None:
                blocks.append(cur)
                if len(blocks) >= MAX_VEVENTS:
                    break
            cur = None
        elif cur is None:
            continue
        elif up == "BEGIN:VALARM":
            in_alarm = True
        elif up == "END:VALARM":
            in_alarm = False
        elif not in_alarm:
            prop = parse_content_line(line)
            if not prop:
                continue
            name, params, value = prop
            if name == "EXDATE":
                cur["EXDATE"].append((params, value))
            elif name not in cur:
                cur[name] = (params, value)
    return blocks


def parse_ics(text, lo_ep, hi_ep):
    """Pure. -> (events, notes). events: [{summary, start (date or datetime), all_day}] that start in [lo_ep, hi_ep), repeating events expanded, cancelled ones and
    replaced instances dropped. notes: short strings about what could not be understood."""
    blocks = _vevents(text)
    notes = []
    overridden = {}
    for b in blocks:                                                # instances the calendar replaced or cancelled must not also come from the repeat rule
        if "RECURRENCE-ID" in b and "UID" in b:
            try:
                ep = _safe_epoch(parse_dt(b["RECURRENCE-ID"][1], b["RECURRENCE-ID"][0]))
            except ValueError:
                ep = None
            if ep is not None:
                overridden.setdefault(b["UID"][1], set()).add(round(ep))
    out, unsupported, bad = [], 0, 0
    for b in blocks:
        if b.get("STATUS", ({}, ""))[1].strip().upper() == "CANCELLED" or "DTSTART" not in b or "SUMMARY" not in b:
            continue
        try:
            start = parse_dt(b["DTSTART"][1], b["DTSTART"][0])
        except ValueError:
            bad += 1
            continue
        summary = _clean(_unescape(b["SUMMARY"][1]))
        if not summary:
            continue
        occurrences = []
        if "RRULE" in b and "RECURRENCE-ID" not in b:
            rule = parse_rrule(b["RRULE"][1])
            if rule_supported(rule, start):
                skip = set(overridden.get(b.get("UID", (None, None))[1], ()))
                for params, value in b["EXDATE"]:
                    for piece in value.split(","):
                        try:
                            ex = _safe_epoch(parse_dt(piece, params))
                        except ValueError:
                            continue
                        if ex is not None:
                            skip.add(round(ex))
                occurrences = [c for c, ep in expand(start, rule, lo_ep, hi_ep) if round(ep) not in skip]
            else:
                unsupported += 1
                ep = _safe_epoch(start)
                occurrences = [start] if ep is not None and lo_ep <= ep < hi_ep else []
        else:
            ep = _safe_epoch(start)
            occurrences = [start] if ep is not None and lo_ep <= ep < hi_ep else []
        for occ in occurrences:
            out.append({"summary": summary, "start": occ, "all_day": not isinstance(occ, dt.datetime)})
    if unsupported:
        notes.append("%d repeating event(s) used a rule that is not understood and count once" % unsupported)
    if bad:
        notes.append("%d event(s) had a date that could not be read" % bad)
    out.sort(key=lambda e: _safe_epoch(e["start"]) or 0)
    return out[:MAX_CACHED], notes


# ---------- config, source, cache ----------
def load_config(logs=None):
    """-> (config, problem). config is {ics_path, ics_url, owner_name} (empty strings when absent) or None; `problem` says what to fix (no prefix)."""
    p = Path(logs or logs_dir()) / CONFIG
    hint = 'create logs/%s containing {"ics_path": "/full/path/to/calendar.ics"} or {"ics_url": "https://..."} (optional "owner_name")' % CONFIG
    try:
        if p.stat().st_size > MAX_CONFIG_BYTES:
            return None, "logs/%s is too large" % CONFIG
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, hint
    except (OSError, ValueError):
        return None, "logs/%s is not valid JSON" % CONFIG
    if not isinstance(raw, dict):
        return None, "logs/%s must hold a JSON object" % CONFIG
    cfg = {k: (raw.get(k).strip() if isinstance(raw.get(k), str) else "") for k in ("ics_path", "ics_url")}
    owner = raw.get("owner_name")
    cfg["owner_name"] = _clean(owner, 40) if isinstance(owner, str) else ""
    if not cfg["ics_path"] and not cfg["ics_url"]:
        return None, hint
    if cfg["ics_url"] and not re.match(r"(?i)(https?|webcal)://", cfg["ics_url"]):
        return None, "ics_url in logs/%s must start with https:// (or webcal://)" % CONFIG
    return cfg, ""


def source_key(cfg):
    """A short fingerprint of the calendar source, so a changed source never reuses another calendar's cache. Never contains the link itself."""
    return hashlib.sha1((cfg.get("ics_path") or cfg.get("ics_url") or "").encode("utf-8", "replace")).hexdigest()[:12]


def _download(url):
    """The only network call: GET the calendar link (https or http), at most MAX_ICS_BYTES + 1 bytes. Tests replace this."""
    import urllib.request
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    req = urllib.request.Request(url, headers={"User-Agent": "assistant-kit-sense-life/1"})
    with urllib.request.urlopen(req, timeout=20) as resp:                  # noqa: S310 - the scheme is checked in load_config and again below
        if not str(resp.geturl()).lower().startswith(("http://", "https://")):
            raise ValueError("unexpected redirect target")
        return resp.read(MAX_ICS_BYTES + 1)


def read_source(cfg):
    """The calendar text from the configured file or link. Raises on any failure (the caller keeps the cache)."""
    if cfg.get("ics_path"):
        p = Path(os.path.expanduser(cfg["ics_path"]))
        if not p.is_absolute():
            p = ROOT / p
        with p.open("rb") as f:
            data = f.read(MAX_ICS_BYTES + 1)
    else:
        data = _download(cfg["ics_url"])
    if len(data) > MAX_ICS_BYTES:
        raise ValueError("the calendar is larger than 5 MB")
    return data.decode("utf-8-sig", errors="replace")


def fetch_events(now, cfg):
    """Impure. Events between 1 day ago and 3 days ahead as plain dicts {summary, start (iso text), all_day}. Raises on a failure; the caller keeps the cache."""
    events, _notes = parse_ics(read_source(cfg), now - LOOKBACK_SEC, now + LOOKAHEAD_SEC)
    return [{"summary": e["summary"], "start": e["start"].isoformat(), "all_day": e["all_day"]} for e in events]


def _revive(rows):
    ev = []
    for r in rows if isinstance(rows, list) else []:
        try:
            if r.get("all_day"):
                s = dt.date.fromisoformat(str(r["start"])[:10])
            else:
                s = dt.datetime.fromisoformat(str(r["start"]).replace("Z", "+00:00"))
        except (KeyError, ValueError, AttributeError, TypeError):
            continue
        ev.append({"summary": r.get("summary", ""), "start": s})
    return ev


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def load_events(now, logs=None, fetch=None, cache_sec=CACHE_SEC, key="", write=True):
    """-> (events, note). A cache younger than `cache_sec` seconds (and made for the same source) is used; otherwise `fetch(now)` runs. A failed refresh keeps the old
    cache; the note then starts with 'refresh failed'. write=False (a dry run) never writes the cache."""
    path = Path(logs or logs_dir()) / CACHE
    cache = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("source", "") == key:
            cache = data
    except (OSError, ValueError):
        pass
    try:
        fetched = float(cache.get("fetched", 0))
    except (TypeError, ValueError):
        fetched = 0.0
    if cache and cache_sec > 0 and 0 <= now - fetched < cache_sec:
        return _revive(cache.get("events", [])), "cache %d min old" % ((now - fetched) // 60)
    try:
        rows = fetch(now)
        if write:
            try:
                _write_json(path, {"fetched": now, "source": key, "events": rows})
            except OSError:
                pass                                                       # a cache that cannot be written only costs a refetch next time
        return _revive(rows), "fetched %d events" % len(rows)
    except Exception as e:                                                 # noqa: BLE001 - a failed source keeps the old cache and never raises
        return _revive(cache.get("events", [])), "refresh failed (%s), %s" % (type(e).__name__, "using the old cache" if cache else "nothing cached yet")


# ---------- state (alert throttle) ----------
def _load_state(logs=None):
    try:
        d = json.loads((Path(logs or logs_dir()) / STATE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def alert_due(state, key, now, every=ALERT_EVERY_SEC):
    """True when the problem `key` has not been alerted within `every` seconds; records the time in state["alerted"] when it returns True."""
    alerted = state.get("alerted")
    if not isinstance(alerted, dict):
        alerted = state["alerted"] = {}
    try:
        last = float(alerted[key]) if key in alerted else None
    except (TypeError, ValueError):
        last = None
    if last is not None and now - last < every:
        return False
    alerted[key] = now
    return True


# ---------- main ----------
def _run(dry, now):
    logs = logs_dir()
    if not dry and (logs / OFF).exists():
        print("sense_life: off")
        return 0
    cfg, problem = load_config(logs)
    if cfg is None:
        print("set up sense_life: " + problem)
        return 0
    events, note = load_events(now, logs, lambda n: fetch_events(n, cfg), cache_sec=0 if cfg["ics_path"] else CACHE_SEC, key=source_key(cfg), write=not dry)
    found = due(events, now, cfg["owner_name"] or None)
    if dry:
        print("%s; %d event(s) in the window" % (note, len(events)))
        for kind, what, tier, value, dedupe, nb, dl in found:
            print("would raise v%d %-15s %s" % (value, kind, what[:110]))
        print("%d finding(s), nothing written" % len(found))
        return 0
    import intent_store
    lg = str(logs)
    logs.mkdir(parents=True, exist_ok=True)
    intents = intent_store.read_jsonl(intent_store.INTENTS, lg)
    added = 0
    for kind, what, tier, value, dedupe, nb, dl in found:
        cand = intent_store.new_intent("life_" + kind, what, tier=tier, value=value, dedupe=dedupe, deadline=intent_store.iso(dl), not_before=intent_store.iso(nb), from_event=kind)
        if not intent_store.is_duplicate(intents, cand):
            intent_store.append(intent_store.EVENTS, {"ts": intent_store.iso(), "source": "life", "kind": kind, "data": {"what": what[:160]}}, lg)
            intents.append(cand)
            added += 1
    if added:
        intent_store.write_intents(intents, lg)
    if note.startswith("refresh failed"):
        state = _load_state(logs)
        if alert_due(state, "refresh", now):
            import notify
            notify.alert("sense_life: " + note, source="sense_life", logs=lg)
            _write_json(logs / STATE, state)
    print("sense_life: %s; %d finding(s), %d new intent(s)" % (note, len(found), added))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sense_life.py", description="raise work items for birthdays and upcoming events from a calendar file or link")
    ap.add_argument("--dry-run", action="store_true", help="print what would be raised, write nothing")
    args = ap.parse_args(argv)
    try:
        return _run(args.dry_run, time.time())
    except Exception as e:                                                 # noqa: BLE001 - an optional sensor must never break the job runner
        print("sense_life: stopped (%s)" % type(e).__name__)
        return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
