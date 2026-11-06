# -*- coding: utf-8 -*-
"""Mail sensor (OPTIONAL, off until you connect Gmail): raises one work item per unread message that LOOKS like it needs an answer, never a bare count.

Why: mail that needs an answer only gets noticed when someone happens to open the inbox, and a count ("12 unread") says nothing about which one matters.
This reads the unread inbox mail of the last 3 days (live and read-only, headers only: From, Subject, Date and a few bulk-mail markers) and keeps a message when
its subject has a question mark or a deadline word (due, urgent, deadline, reply, asap, rsvp, "by Friday", ...), or when it comes from a sender domain you listed as
important. Newsletters, promotions, receipts, no-reply senders and bulk mail are dropped unless the sender is important AND the subject carries a real signal.
Tier 2 (an answer is the user's call), value 3, at most 8 per run, newest first.
It perceives only: it never sends, replies, opens, marks or deletes mail and contacts nobody. Mail text is untrusted DATA: only the sender name (30 chars) and at
most 80 characters of subject reach the work item, quoted, with control characters stripped; bodies and snippets are never read and nothing in a subject is ever
acted on. Links and attachments are never opened.

Setup (once): connect Gmail read-only with gmail_api.py (see its docstring: python scripts/gmail_api.py auth). Until then this prints one
'set up sense_mail: ...' line and exits 0. Optional file logs/sense_mail_config.json:
    {"important_domains": ["example.org"], "lookback_days": 3, "max_items": 8}
    python sense_mail.py [--dry-run]
--dry-run prints "would raise ..." and writes nothing, and runs even while the kill switch exists. Kill switch: logs/sense_mail_off.txt.
Seen list: logs/sense_mail_state.json (keys already raised, newest 500), so one mail is never raised twice. A Gmail problem (revoked grant, network) is reported once
every 6 hours through notify.alert and the run still exits 0.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): needs_reply(gmail_rows, now, seen, cfg), plus _row_from_message, _is_bulk and load_config."""
import argparse
import email.utils
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gmail_api  # noqa: E402  (same folder; the Google libraries are only imported when a call needs them)

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

OFF = "sense_mail_off.txt"
STATE = "sense_mail_state.json"
CONFIG = "sense_mail_config.json"
LOOKBACK_DAYS = 3
MAX_ITEMS = 8
SUBJECT_MAX = 80
SENDER_MAX = 30
VALUE = 3
SEEN_KEEP = 500
FETCH_BUDGET_SEC = 60
ALERT_EVERY_SEC = 6 * 3600
MAX_CONFIG_BYTES = 64 * 1024
GMAIL_QUERY = "in:inbox is:unread newer_than:%dd -category:promotions -category:social -category:forums"
HEADERS = ["From", "Subject", "List-Unsubscribe", "Precedence", "Auto-Submitted"]

NOISE_LABELS = frozenset(("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "CATEGORY_UPDATES", "SPAM"))
NOISE_SENDER = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|notification|mailer-daemon|postmaster|bounce|newsletter|marketing|promo|receipt|alert|digest|banking"
                          r"|(^|[._+-])(news|offers|deals|billing|orders)($|[._+-])", re.I)
NOISE_SUBJECT = re.compile(r"receipt|invoice|order\s*(#|no\b|confirm)|your order|statement|transaction|verification code|one[- ]time|\botp\b|(reset|changed?|forgot|your)\s+(your\s+)?password|password\s+(reset|change)|security alert"
                           r"|newsletter|webinar|\d+\s*% off|\bsale\b|\bdeals?\b|promo|coupon|unsubscribe|subscription|welcome to|payslip|booking confirm"
                           r"|thank you for (your )?(order|purchase|signing)", re.I)
WHEN = (r"today|tonight|tomorrow|eod|noon|midnight|mon(day)?|tue(s|sday)?|wed(nesday)?|thu(r|rs|rsday)?|fri(day)?|sat(urday)?|sun(day)?"
        r"|\d{1,2}(:\d\d)?\s*(am|pm)|\d{1,2}[/-]\d{1,2}|\d{1,2}(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
        r"|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d")
DEADLINE = re.compile(r"\b(due(?!\s+to\b)|urgent|deadline|reply|asap|rsvp|action required|response required|respond by)\b|\bby\s+(%s)\b" % WHEN, re.I)
DOMAIN_OK = re.compile(r"[a-z0-9][a-z0-9.-]{0,98}[a-z0-9]")


def logs_dir():
    return Path(LOGS)


def _clean(text, limit):
    """Pure. Untrusted text made safe to quote: control characters and newlines become spaces, double quotes become single, whitespace collapses, cut to `limit`."""
    s = "".join(ch if ch.isprintable() else " " for ch in str(text if text is not None else ""))
    return " ".join(s.replace('"', "'").split())[:limit].strip()


def default_config():
    return {"important_domains": [], "lookback_days": LOOKBACK_DAYS, "max_items": MAX_ITEMS}


def load_config(logs=None):
    """-> the optional settings with defaults filled in. A missing, oversized, unreadable or malformed file gives the defaults; bad single values are ignored."""
    cfg = default_config()
    p = Path(logs or logs_dir()) / CONFIG
    try:
        if p.stat().st_size > MAX_CONFIG_BYTES:
            return cfg
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    domains = []
    listed = raw.get("important_domains")
    for d in listed if isinstance(listed, list) else []:
        d = str(d).strip().lower().lstrip("@").strip(".")
        if DOMAIN_OK.fullmatch(d) and d not in domains:
            domains.append(d)
    cfg["important_domains"] = domains[:50]
    for key, lo, hi in (("lookback_days", 1, 14), ("max_items", 1, 20)):
        v = raw.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
            cfg[key] = v
    return cfg


def _when(row):
    """Pure. A row's time as an epoch: the server-side `epoch` if present, else its RFC 2822 `date`; None when neither is usable."""
    ep = row.get("epoch")
    if isinstance(ep, (int, float)) and not isinstance(ep, bool):
        return float(ep)
    d = row.get("date")
    if d:
        try:
            return email.utils.parsedate_to_datetime(str(d)).timestamp()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return None


def _in_window(ep, now, days=LOOKBACK_DAYS):
    """Pure. True for a time within the last `days` days (inclusive); a time more than an hour in the future is not trusted."""
    return ep is not None and -3600 <= now - ep <= days * 86400


def _important(addr, domains=()):
    domain = (addr or "").rpartition("@")[2].lower()
    return bool(domain) and any(domain == d or domain.endswith("." + d) for d in domains)


def _has_signal(subject):
    """Pure. True when a subject asks a question or carries a deadline word."""
    return "?" in subject or "？" in subject or bool(DEADLINE.search(subject))


def _is_bulk(headers):
    """Pure. True when the headers mark mass or automatic mail (List-Unsubscribe, Precedence bulk/list/junk, Auto-Submitted other than 'no')."""
    h = {str(k).lower(): str(v).strip().lower() for k, v in (headers or {}).items()}
    return "list-unsubscribe" in h or h.get("precedence") in ("bulk", "list", "junk") or ("auto-submitted" in h and h["auto-submitted"] != "no")


def _row_from_message(msg):
    """Pure. A Gmail API metadata message as the row the core reads: id, from, subject, epoch (server receive time), labels, bulk. No snippet, no body."""
    if not isinstance(msg, dict):
        return None
    try:
        hdrs = {h.get("name", ""): h.get("value", "") for h in (msg.get("payload") or {}).get("headers", []) if isinstance(h, dict)}
    except AttributeError:
        hdrs = {}
    low = {k.lower(): v for k, v in hdrs.items()}
    try:
        epoch = int(msg.get("internalDate")) / 1000.0
    except (TypeError, ValueError):
        epoch = None
    return {"id": msg.get("id"), "from": low.get("from", ""), "subject": low.get("subject", ""), "epoch": epoch,
            "labels": list(msg.get("labelIds") or []), "bulk": _is_bulk(hdrs)}


def _gmail_item(row, now, have, cfg):
    """Pure. (epoch, tuple) for a Gmail row that looks like it needs an answer, else None."""
    if not isinstance(row, dict) or not row.get("id"):
        return None
    key = "mail-gmail-%s" % row["id"]
    if key in have:
        return None
    ep = _when(row)
    if not _in_window(ep, now, cfg["lookback_days"]):
        return None
    subject = str(row.get("subject") or "")
    name, addr = email.utils.parseaddr(str(row.get("from") or ""))
    local = addr.rpartition("@")[0]
    important = _important(addr, cfg["important_domains"])
    signal = _has_signal(subject)
    labels = row.get("labels") or []
    noisy = bool(row.get("bulk")) or bool(NOISE_LABELS.intersection(labels if isinstance(labels, (list, tuple, set)) else [])) \
        or bool(NOISE_SENDER.search(local)) or bool(NOISE_SUBJECT.search(subject))
    if not ((important and signal) if noisy else (important or signal)):
        return None
    what = 'gmail from %s: "%s"' % (_clean(name or addr, SENDER_MAX) or "unknown sender", _clean(subject, SUBJECT_MAX) or "(no subject)")
    return ep, (what, VALUE, key, None, None)


def needs_reply(gmail_rows, now, seen=None, cfg=None):
    """Pure. -> [(what, value, dedupe, not_before, deadline)], newest first, at most cfg["max_items"]: one entry per message that looks like it needs an answer
    (never a count). Anything whose dedupe key is in `seen`, older than the look-back window, or noise is left out; junk rows are skipped."""
    cfg = dict(default_config(), **(cfg or {}))
    have = set(seen or [])
    found = []
    for row in gmail_rows or []:
        item = _gmail_item(row, now, have, cfg)
        if item:
            have.add(item[1][2])
            found.append(item)
    found.sort(key=lambda it: -it[0])
    return [t for _ep, t in found[:cfg["max_items"]]]


def _fetch_gmail(days=LOOKBACK_DAYS, limit=50):
    """-> (rows, note). Live, read-only: the unread inbox headers of the last `days` days. Never starts the consent flow and never rewrites the token file; any
    failure comes back as ([], note)."""
    try:
        msgs, partial = gmail_api.list_messages_metadata(GMAIL_QUERY % days, limit, HEADERS, budget_sec=FETCH_BUDGET_SEC)
    except gmail_api.GmailError as e:
        return [], "gmail unavailable: %s" % e
    except Exception as e:                                          # noqa: BLE001 - a sensor reports a failed source, it never crashes on one
        return [], "gmail unavailable: %s" % type(e).__name__
    rows = [r for r in (_row_from_message(m) for m in msgs) if r]
    return rows, ("gmail partial: stopped reading after %ds" % FETCH_BUDGET_SEC) if partial else ""


def _load_state(logs=None):
    try:
        d = json.loads((Path(logs or logs_dir()) / STATE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("seen"), list) else {"seen": []}
    except (OSError, ValueError):
        return {"seen": []}


def _save_state(state, logs=None):
    lg = Path(logs or logs_dir())
    lg.mkdir(parents=True, exist_ok=True)
    state["seen"] = list(state.get("seen", []))[-SEEN_KEEP:]
    tmp = lg / (STATE + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, lg / STATE)


def alert_due(state, key, now, every=ALERT_EVERY_SEC):
    """Pure-ish. True when the problem `key` has not been alerted within `every` seconds; records the time in state["alerted"] when it returns True."""
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


def _run(dry, now):
    logs = logs_dir()
    if not dry and (logs / OFF).exists():
        print("sense_mail: off")
        return 0
    ok, why = gmail_api.configured()
    if not ok:
        print("set up sense_mail: " + why)
        return 0
    cfg = load_config(logs)
    rows, note = _fetch_gmail(cfg["lookback_days"])
    state = _load_state(logs)
    found = needs_reply(rows, now, state.get("seen", []), cfg)
    if dry:
        for what, value, _dedupe, _nb, _dl in found:
            print("would raise tier2 v%d %s" % (value, what))
        if note:
            print("note: " + note)
        print("%d finding(s), nothing written" % len(found))
        return 0
    import intent_store
    lg = str(logs)
    logs.mkdir(parents=True, exist_ok=True)
    intents = intent_store.read_jsonl(intent_store.INTENTS, lg)
    intent_store.expire_leases(intents)
    added = 0
    for what, value, dedupe, nb, dl in found:
        intent_store.append(intent_store.EVENTS, {"ts": intent_store.iso(), "source": "mail", "kind": "needs_reply", "data": {"what": what, "value": value, "src": "gmail"}}, lg)
        cand = intent_store.new_intent("mail_needs_reply", what, tier=2, value=value, dedupe=dedupe, deadline=dl, not_before=nb, from_event="needs_reply")
        if intent_store.is_duplicate(intents, cand):
            continue
        intents.append(cand)
        added += 1
    if added:
        intent_store.write_intents(intents, lg)
    dirty = False
    if found:
        state["seen"] = list(state.get("seen", [])) + [t[2] for t in found]
        dirty = True
    if note and alert_due(state, "fetch", now):
        import notify
        notify.alert("sense_mail: " + note, source="sense_mail", logs=lg)
        dirty = True
    if dirty:
        _save_state(state, logs)
    print("sense_mail: %d finding(s), %d new intent(s)" % (len(found), added))
    if note:
        print("sense_mail: " + note, file=sys.stderr)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sense_mail.py", description="raise a work item per unread Gmail message that needs an answer (read-only)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be raised, write nothing")
    args = ap.parse_args(argv)
    try:
        return _run(args.dry_run, time.time())
    except Exception as e:                                          # noqa: BLE001 - an optional sensor must never break the job runner
        print("sense_mail: stopped (%s)" % type(e).__name__)
        return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
