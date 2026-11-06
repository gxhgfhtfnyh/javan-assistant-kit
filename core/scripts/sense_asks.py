# -*- coding: utf-8 -*-
"""Ask sensor (OPTIONAL, off until you give it your message log): nothing the user asks falls through.

Why: an ask lived only in the conversation. If the assistant answered "ok doing it" and then the context was compacted, the session died, or it got pulled onto something
else, the ask was gone and the user had to ask again ("what happened to that"). This reads the user's own messages and the assistant's replies from two message logs
and raises a work item for every message that asks for something and is still unanswered or only acknowledged after 10 minutes:
  - ask-like: a request, a question or a directive ("can you", "please", "make sure", a question mark, an imperative opener), 4+ words;
  - answered: the assistant sent a real reply to it (8+ words, and not just an announcement of what it is about to do); a bare acknowledgement does not count;
  - only messages that arrived AFTER this sensor was first run are considered (logs/sense_asks_state.json holds that start time), so history is never replayed.
The user's own text goes into the work item (cut to 140 characters, control characters stripped, quoted). It perceives only: it replies to nobody.
Tier 1, value 5; one item per message (dedupe on the message id), at most 10 per run.

Setup (once): tell it where the logs are. There is NO default path. Create logs/message_log_config.json:
    {"incoming_log": "logs/user_messages.jsonl",
     "outgoing_log": "logs/assistant_messages.jsonl",
     "fields": {"id": "id", "text": "text", "time": "date", "reply_to": "reply_to"},
     "only": {"author_id": "12345"},
     "match": "reply_to"}
Each log is JSON Lines: one object per line, one line per message. `incoming_log` holds what the user wrote, `outgoing_log` what the assistant sent; a relative path is read
from the assistant folder. `fields` renames the keys if yours differ (the defaults are shown; time may be an ISO 8601 text or a Unix time). `only` is optional: keep only
rows whose fields equal these values (use it when the incoming log also holds other people's messages). `match` is "reply_to" (a reply names the message id it answers,
the default) or "next_reply" (any later substantive reply answers every earlier ask, for logs without reply ids). At most the last 4 MB of each log is read.
Until the file exists this prints one 'set up sense_asks: ...' line and exits 0.
    python sense_asks.py [--dry-run]
Kill switch: logs/sense_asks_off.txt (a dry run ignores it). Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): is_ask(text), answered(msg, replies, match), open_asks(incoming, replies, now, since, match)."""
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

OFF = "sense_asks_off.txt"
STATE = "sense_asks_state.json"
CONFIG = "message_log_config.json"
GRACE = 10 * 60
WINDOW = 24 * 3600
MAX_PER_RUN = 10
RAISED_KEEP = 500
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_ROWS = 5000
MAX_CONFIG_BYTES = 64 * 1024
TEXT_MAX = 140
DEFAULT_FIELDS = {"id": "id", "text": "text", "time": "date", "reply_to": "reply_to"}
MATCH_MODES = ("reply_to", "next_reply")

ASK_RX = re.compile(r"\?|^\W*(what|whats|what's|when|where|why|how|which|who|is|are|does|did|has|have|any|can|could|would|will|pls|plz|please|make|build|fix|check|find|send|set|remind|research|look|write|add|remove|update|run|open|create|do|go|keep|stop|start|try|show|tell|give|get|put|use|delete|push|redo|continue)\b"
                    r"|\b(can|could|would) (u|you)\b|\b(you|u) (should|gotta|have to|need to|must)\b|(?<!think )\bi (want|need)\b|\bmake sure\b|\bremember to\b|\bdon'?t forget\b|\bwe should\b|\blet'?s\b|\bplease\b", re.I)
# things that look like asks but are not: "please note/be aware/disregard", exclamations that open with a question word, a tag question,
# "i need a break", "i think i need ..."
NOT_ASK = re.compile(r"^\W*please\s+(note|be aware|disregard|ignore|excuse|forgive|bear)\b|^\W*(what a|what the|who knew|how nice|how cool|why would anyone)\b"
                     r"|\b(right|yeah|ya|no|huh|eh)\s*\?\s*$|\bi (?:really |just |kinda )?need (?:a |an |some |to )?(?:break|nap|sleep|rest|coffee|food|snack|shower|water|vacation|holiday|hug|minute|moment)\b"
                     r"|\bi think i (?:need|want)\b", re.I)
ACK_ONLY = re.compile(r"^\W*(ok|okay|k|yes|yep|yeah|no|nope|thanks|thank you|thx|nice|cool|lol|lmao|gn|good night|bet|sure|got it|alright|fair)\W*$", re.I)
NARRATION = re.compile(r"\b(i'?m (now |currently |just )?(working on|starting|building|about to|going to)|i'?ll (start|begin|do|build|work on|get started)|let me (start|begin|get|do|build|work)|on it)\b", re.I)


def logs_dir():
    return Path(LOGS)


def _clean(text, limit):
    """Pure. Untrusted text made safe to quote: control characters become spaces, double quotes become single, whitespace collapses, cut to `limit`."""
    s = "".join(ch if ch.isprintable() else " " for ch in str(text if text is not None else ""))
    return " ".join(s.replace('"', "'").split())[:limit].strip()


# ---------- the pure core ----------
def is_ask(text):
    """Pure. Whether a message of the user's asks for something (a request, question or directive)."""
    t = " ".join((text or "").split())
    if len(t.split()) < 4 or ACK_ONLY.match(t):
        return False
    return bool(ASK_RX.search(t)) and not NOT_ASK.search(t)


def substantive(text):
    """Pure. True for a real reply: 8+ words and not only an announcement of what the sender is about to do."""
    words = len((text or "").split())
    return words >= 8 and not (NARRATION.search(text or "") and words < 30)


def answered(msg, replies, match="reply_to"):
    """Pure. True when one of the assistant's replies to this message is a real reply. match="reply_to": the reply names this message's id. match="next_reply": any
    substantive reply sent after the message counts."""
    for r in replies or []:
        if match == "next_reply":
            linked = r.get("epoch") is not None and msg.get("epoch") is not None and r["epoch"] > msg["epoch"]
        else:
            linked = msg.get("id") is not None and r.get("reply_to") == msg.get("id")
        if linked and substantive(r.get("text")):
            return True
    return False


def open_asks(incoming, replies, now, since=0.0, match="reply_to"):
    """Pure. -> [(msg id, text, epoch)] of the user's asks that arrived after `since`, are 10+ minutes old and under a day old, and are not answered yet.
    Rows are the normalized form (see normalize); a row without a usable time is skipped."""
    out = []
    for m in incoming or []:
        t = m.get("epoch")
        if t is None or t <= since or now - t < GRACE or now - t > WINDOW:
            continue
        if is_ask(m.get("text")) and not answered(m, replies, match):
            out.append((m["id"], " ".join((m.get("text") or "").split()), t))
    return out


# ---------- message log rows ----------
def to_epoch(v):
    """Pure. A Unix time (seconds or milliseconds), a numeric string or an ISO 8601 text as epoch seconds; None when it is none of those."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if v > 1e11 else float(v)
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return to_epoch(float(s))
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def normalize(row, fields):
    """Pure. A log row as {id, text, epoch, reply_to}, or None for something that is not an object. A row with no id gets a stable one made from its text and time."""
    if not isinstance(row, dict):
        return None
    text = row.get(fields["text"])
    text = text if isinstance(text, str) else ""
    ep = to_epoch(row.get(fields["time"]))
    rid = row.get(fields["id"])
    if rid is None or rid == "" or isinstance(rid, (dict, list)):
        rid = "h" + hashlib.sha1(("%s|%s" % (text, ep)).encode("utf-8", "replace")).hexdigest()[:10]
    rr = row.get(fields["reply_to"])
    return {"id": str(rid)[:80], "text": text, "epoch": ep, "reply_to": None if rr is None or rr == "" or isinstance(rr, (dict, list)) else str(rr)[:80]}


def keep(row, only):
    """Pure. True when the raw row matches every field=value pair of `only` (compared as text); an empty filter keeps everything."""
    return isinstance(row, dict) and all(str(row.get(k)) == str(v) for k, v in (only or {}).items())


def read_rows(path, max_bytes=MAX_LOG_BYTES, max_rows=MAX_ROWS):
    """-> the last rows of a JSON Lines file as dicts (at most the last `max_bytes` bytes and `max_rows` rows), or None when the file is missing or unreadable."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()                                        # drop the cut-off first line
            data = f.read(max_bytes)
    except OSError:
        return None
    rows = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows[-max_rows:]


def load_config(logs=None):
    """-> (config, problem). config: incoming_log, outgoing_log, fields, only, match. `problem` says what to fix (no prefix)."""
    p = Path(logs or logs_dir()) / CONFIG
    hint = ('create logs/%s with {"incoming_log": "<path>", "outgoing_log": "<path>"}: JSON Lines files of what the user wrote and what the assistant sent '
            '(see the sense_asks.py docstring)' % CONFIG)
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
    for key in ("incoming_log", "outgoing_log"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            return None, "logs/%s needs \"%s\" (a path to a JSON Lines file)" % (CONFIG, key)
    fields = dict(DEFAULT_FIELDS)
    if isinstance(raw.get("fields"), dict):
        for k, v in raw["fields"].items():
            if k in DEFAULT_FIELDS and isinstance(v, str) and v.strip():
                fields[k] = v.strip()
    only = {}
    if isinstance(raw.get("only"), dict):
        only = {str(k): v for k, v in raw["only"].items() if isinstance(v, (str, int, float)) and not isinstance(v, bool)}
    match = raw.get("match") if raw.get("match") in MATCH_MODES else "reply_to"
    return {"incoming_log": raw["incoming_log"].strip(), "outgoing_log": raw["outgoing_log"].strip(), "fields": fields, "only": only, "match": match}, ""


def resolve_log(raw):
    p = Path(os.path.expanduser(raw))
    return p if p.is_absolute() else ROOT / p


def _load_state(logs=None):
    try:
        d = json.loads((Path(logs or logs_dir()) / STATE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state, logs=None):
    lg = Path(logs or logs_dir())
    lg.mkdir(parents=True, exist_ok=True)
    state["raised"] = list(state.get("raised", []))[-RAISED_KEEP:]
    tmp = lg / (STATE + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, lg / STATE)


def collect(cfg, now, since):
    """-> (found, notes). Reads both logs and returns the open asks."""
    inc_raw = read_rows(resolve_log(cfg["incoming_log"]))
    out_raw = read_rows(resolve_log(cfg["outgoing_log"]))
    notes = []
    if inc_raw is None:
        notes.append("incoming log not found yet")
    if out_raw is None:
        notes.append("outgoing log not found yet")
    f = cfg["fields"]
    incoming = [n for n in (normalize(r, f) for r in (inc_raw or []) if keep(r, cfg["only"])) if n]
    replies = [n for n in (normalize(r, f) for r in (out_raw or [])) if n]
    return open_asks(incoming, replies, now, since, cfg["match"]), notes


def _run(dry, now):
    logs = logs_dir()
    if not dry and (logs / OFF).exists():
        print("sense_asks: off")
        return 0
    cfg, problem = load_config(logs)
    if cfg is None:
        print("set up sense_asks: " + problem)
        return 0
    state = _load_state(logs)
    try:
        since = float(state.get("since"))
    except (TypeError, ValueError):
        since = None
    first_run = since is None
    found, notes = ([], []) if first_run else collect(cfg, now, since)
    if dry:
        print("since: %s" % ("not set yet (first real run records it)" if first_run else time.strftime("%F %T", time.localtime(since))))
        for mid, text, t in found:
            print("would raise v5 ask_open  %s  %s" % (time.strftime("%H:%M", time.localtime(t)), _clean(text, 100)))
        print("%d finding(s), nothing written" % len(found))
        return 0
    if first_run:
        _save_state({"since": now, "raised": []}, logs)
        print("sense_asks: armed at %s, nothing earlier is replayed" % time.strftime("%F %T", time.localtime(now)))
        return 0
    import intent_store
    lg = str(logs)
    intents = intent_store.read_jsonl(intent_store.INTENTS, lg)
    raised = list(state.get("raised", [])) if isinstance(state.get("raised"), list) else []
    have = {i.get("dedupe") for i in intents} | {"ask-%s" % r for r in raised}      # an ask that was ever raised, in any state, is never raised a second time
    added = 0
    for mid, text, t in found:
        key = "ask-%s" % mid
        if key in have or added >= MAX_PER_RUN:
            continue
        shown = _clean(text, TEXT_MAX)
        what = 'The user asked at %s (msg %s): "%s" and it is not answered or finished. Do it, or tell them the one thing only they can do; close with evidence.' % (
            time.strftime("%H:%M", time.localtime(t)), _clean(mid, 40), shown)
        intent_store.append(intent_store.EVENTS, {"ts": intent_store.iso(), "source": "asks", "kind": "ask_open", "data": {"id": _clean(mid, 40), "text": shown}}, lg)
        intents.append(intent_store.new_intent("ask_open", what, tier=1, value=5, dedupe=key, from_event="ask_open"))
        raised.append(mid)
        added += 1
    if added:
        intent_store.write_intents(intents, lg)
        state["raised"] = raised
        _save_state(state, logs)
    print("sense_asks: %d finding(s), %d new intent(s)%s" % (len(found), added, (" (" + ", ".join(notes) + ")") if notes else ""))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sense_asks.py", description="raise a work item for a user request that stays unanswered")
    ap.add_argument("--dry-run", action="store_true", help="print what would be raised, write nothing")
    args = ap.parse_args(argv)
    try:
        return _run(args.dry_run, time.time())
    except Exception as e:                                                 # noqa: BLE001 - an optional sensor must never break the job runner
        print("sense_asks: stopped (%s)" % type(e).__name__)
        return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
