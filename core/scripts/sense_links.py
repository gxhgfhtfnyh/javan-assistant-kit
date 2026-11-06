# -*- coding: utf-8 -*-
"""Link sensor: notices a link the owner sent (over the Discord DM channel, see discord_daemon.py) that nobody has looked at yet.

Why this one: "research this" / "study this link" is a common ask once a chat channel is wired up, and a shared link usually sits unread until
someone happens to reopen the DM. A sensor that notices an unread link the moment it arrives turns that into something already under way.

It reads the Discord incoming log (logs/discord_incoming.jsonl, written by discord_daemon.py), keeps the ids it has already raised in
logs/sense_links_state.json, and raises ONE intent per unprocessed link (tier 1: read it and report what it changes; tier 2 for a host that
needs the owner's own account there, since that is their call, not the assistant's). It never opens a link itself: perceive only.
OPTIONAL: does nothing until logs/discord_config.json has an owner_id (see discord_daemon.py) - until then it prints a one-line setup note and exits 0.
Kill switch: logs/sense_links_off.txt.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
    python sense_links.py [--dry-run] [--since-hours 48]
Pure core (unit tested): links_in(text), classify(url), unprocessed(rows, seen, owner_id, cutoff)."""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.environ.get("HARNESS_ROOT") or os.path.dirname(HERE)
LOGS = os.environ.get("HARNESS_LOGS") or os.path.join(ROOT, "logs")
STATE = "sense_links_state.json"
OFF = "sense_links_off.txt"
URL = re.compile(r"https?://[^\s<>()\[\]]+", re.I)
# host fragment -> (what reading it means, tier); a host that needs the owner's own signed-in account is their call, not the assistant's
KINDS = (("youtu", ("video", 1)), ("youtube.com", ("video", 1)), ("reddit.com", ("reddit thread", 1)), ("github.com", ("repo or file", 1)),
         ("arxiv.org", ("paper", 1)), ("anthropic.com", ("docs page", 1)), ("claude.com", ("docs page", 1)),
         ("instagram.com", ("instagram post (needs the owner's account)", 2)), ("facebook.com", ("facebook post (needs the owner's account)", 2)),
         ("chatgpt.com", ("shared chatgpt chat (needs the owner's account)", 2)), ("x.com", ("post (needs the owner's account)", 2)),
         ("twitter.com", ("post (needs the owner's account)", 2)))


def links_in(text):
    """Every http(s) link in a message, trailing punctuation stripped, in order and without repeats."""
    out = []
    for u in URL.findall(text or ""):
        u = u.rstrip(".,;:!?'\"")
        if u not in out:
            out.append(u)
    return out


def classify(url):
    """(what it is, tier) for a link; anything unknown is a page that may be read and reported on."""
    host = (url or "").split("//", 1)[-1].split("/", 1)[0].lower()
    for frag, kind in KINDS:
        if frag in host:
            return kind
    return ("page", 1)


def _epoch(date_str):
    try:
        return time.mktime(time.strptime((date_str or "")[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except Exception:
        return 0.0


def unprocessed(rows, seen, owner_id=None, cutoff=0.0):
    """Pure. -> [(url, message id, epoch)] for links the owner sent after cutoff whose url is not in `seen`, oldest first, no duplicate urls.
    `owner_id` has no default: without a real id (nothing configured yet) nothing can match, which is the correct behaviour."""
    out, have = [], set(seen or [])
    for m in rows:
        if not isinstance(m, dict) or owner_id is None or m.get("author_id") != owner_id or m.get("source") != "dm":
            continue
        ts = _epoch(m.get("date"))
        if ts < cutoff:
            continue
        for u in links_in(m.get("text", "")):
            if u in have:
                continue
            have.add(u)
            out.append((u, m.get("id"), ts))
    return sorted(out, key=lambda t: t[2])


def _load_state(logs=None):
    try:
        return json.load(open(os.path.join(logs or LOGS, STATE), encoding="utf-8"))
    except (OSError, ValueError):
        return {"seen": []}


def _save_state(state, logs=None):
    json.dump(state, open(os.path.join(logs or LOGS, STATE), "w", encoding="utf-8"))


def main(argv=None):
    argv = argv or []
    logs = LOGS
    if os.path.exists(os.path.join(logs, OFF)):
        print("sense_links: off")
        return 0
    import discord_queue_send as dqs
    owner = dqs.owner_id(logs)
    if owner is None:
        print("sense_links: not set up (no owner_id in logs/%s); nothing to check" % dqs.CONFIG_NAME)
        return 0
    hours = int(argv[argv.index("--since-hours") + 1]) if "--since-hours" in argv else 48
    import intent_store
    rows = intent_store.read_jsonl(dqs.INCOMING_NAME, logs)
    state = _load_state(logs)
    found = unprocessed(rows, state.get("seen", []), owner_id=owner, cutoff=time.time() - hours * 3600)
    if "--dry-run" in argv:
        for u, mid, ts in found:
            what, tier = classify(u)
            print("would raise tier%d %-34s %s" % (tier, what, u[:70]))
        print("%d unread link(s) in the last %dh, nothing written" % (len(found), hours))
        return 0
    intents = intent_store.read_jsonl(intent_store.INTENTS, logs)
    intent_store.expire_leases(intents)
    added = 0
    for u, mid, ts in found:
        what, tier = classify(u)
        intent_store.append(intent_store.EVENTS, {"ts": intent_store.iso(ts), "source": "links", "kind": "unread_link", "data": {"url": u, "msg": mid, "what": what}}, logs)
        cand = intent_store.new_intent("unread_link", "read this %s sent and report what it changes: %s" % (what, u), tier=tier, value=3,
                                       dedupe="link-" + u[-60:], from_event=str(mid))
        if intent_store.is_duplicate(intents, cand):
            continue
        intents.append(cand)
        added += 1
    if added:
        intent_store.write_intents(intents, logs)
    state["seen"] = (state.get("seen", []) + [u for u, _m, _t in found])[-500:]
    _save_state(state, logs)
    print("sense_links: %d unread link(s), %d new intent(s)" % (len(found), added))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
