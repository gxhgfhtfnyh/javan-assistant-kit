# -*- coding: utf-8 -*-
"""Intent store: a machine-readable work queue for a harness.

Three append-only files plus one small state file, all under the logs folder:
    events.jsonl      facts from sensors, no judgement: {ts, id, source, kind, data}
    intents.jsonl     candidate actions: {id, ts, kind, what, tier, value, deadline, dedupe, status, shadow, from_event, lease}
    outcomes.jsonl    what happened: {ts, intent_id, action, evidence, result, reaction}
    harness_state.json  {updated, focus, waiting_on, counters}

Tiers: 0 do quietly, 1 do then tell, 2 ask first, 3 never. Only tiers 0 and 1 are auto-actionable;
2 surfaces as a question, 3 is refused. A shadow intent is logged and never acted on.

Nothing here sends, deletes or runs anything: it only records and ranks. A lease (claim with
an expiry) keeps concurrent runners from doing the same intent twice.

Usage:
    python intent_store.py add --kind report_due --what "Quarterly report due tomorrow" --tier 1 --value 4 --dedupe report-due-1031
    python intent_store.py next                 # the top actionable intent, or why there is none
    python intent_store.py claim <id> --by live-session
    python intent_store.py done <id> --result ok --evidence "sent at 02:40, daemon log line"
    python intent_store.py list [--all]

Pure core (unit tested): new_intent, is_duplicate, actionable, rank, next_action, expire_leases, apply_outcome.
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
import threading

EVENTS = "events.jsonl"
INTENTS = "intents.jsonl"
OUTCOMES = "outcomes.jsonl"
STATE = "harness_state.json"


def _logs_dir():
    return os.environ.get("HARNESS_LOGS") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
AUTO_TIERS = (0, 1)              # tiers that may be acted on without asking
LEASE_SEC = 20 * 60
LOCK_STALE = 30                  # seconds after which a lock file nobody released counts as left behind by a crashed holder
LOCK_WAIT = 8                    # seconds to wait for the intents lock before writing anyway
COOLDOWN_SEC = 6 * 3600          # a finished intent with the same dedupe key stays suppressed this long


def now_s():
    return time.time()


def iso(ts=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else now_s()))


def new_intent(kind, what, tier=1, value=3, dedupe=None, deadline=None, from_event=None, shadow=False, ts=None, not_before=None, check=None):
    """A fresh intent dict. id is stable for the same kind+dedupe+minute so a double-fired sensor cannot create two.
    `check` (optional, see parse_check) is a done-when test: `done --result ok` refuses to close the intent until it passes.
    """
    ts = now_s() if ts is None else ts
    key = "%s|%s|%s" % (kind, dedupe or what, int(ts // 60))
    out = {"id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:12], "ts": iso(ts), "epoch": ts, "kind": kind, "what": what,
           "tier": int(tier), "value": int(value), "deadline": deadline, "not_before": not_before, "dedupe": dedupe or kind, "status": "open",
           "shadow": bool(shadow), "from_event": from_event, "lease": None}
    if check:
        out["check"] = check
    return out


SCRIPTS = os.path.dirname(os.path.abspath(__file__))


def parse_check(spec):
    """Pure. 'pytest:test_x.py' | 'dry_run:x.py' | 'exists:C:/path' | 'contains:C:/path::text' -> a check dict, or None when the spec is not one of the closed set.
    Only these four kinds exist and a script or test name must be a bare file name, so a stored check can never run an arbitrary command.
    """
    kind, _, arg = (spec or "").partition(":")
    arg = arg.strip()
    if kind == "pytest" and re.fullmatch(r"test_[A-Za-z0-9_]+\.py", arg):
        return {"kind": "pytest", "file": arg}
    if kind == "dry_run" and re.fullmatch(r"[A-Za-z0-9_]+\.py", arg):
        return {"kind": "dry_run", "script": arg}
    if kind == "exists" and arg:
        return {"kind": "exists", "path": arg}
    if kind == "contains" and "::" in arg:
        path, _, text = arg.partition("::")
        if path and text:
            return {"kind": "contains", "path": path, "text": text}
    return None


def describe_check(check):
    if not check:
        return ""
    k = check.get("kind")
    return {"pytest": "pytest %s passes", "dry_run": "%s --dry-run exits 0", "exists": "%s exists", "contains": "%s contains the text"}.get(k, "%s") % (
        check.get("file") or check.get("script") or check.get("path") or "?")


def run_check(check, run=None, timeout=120, scripts=None):
    """-> (ok, detail). Read-only by construction: the four kinds above only run pytest or a script's own --dry-run from the scripts folder, or look at a file."""
    import subprocess
    run = run or subprocess.run
    scripts = scripts or SCRIPTS
    try:
        k = check.get("kind")
        if k == "exists":
            ok = os.path.exists(check["path"])
            return ok, "present" if ok else "missing: %s" % check["path"]
        if k == "contains":
            text = open(check["path"], encoding="utf-8", errors="replace").read()
            ok = check["text"].lower() in text.lower()
            return ok, "text found" if ok else "text not found in %s" % check["path"]
        if k in ("pytest", "dry_run"):
            name = check.get("file") or check.get("script")
            if os.path.basename(name) != name or not os.path.exists(os.path.join(scripts, name)):
                return False, "no such file in the scripts folder: %s" % name
            if k == "dry_run" and "--dry-run" not in open(os.path.join(scripts, name), encoding="utf-8", errors="replace").read():
                return False, "%s does not mention --dry-run, so running it could do the real thing" % name
            argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", name] if k == "pytest" else [sys.executable, name, "--dry-run"]
            r = run(argv, cwd=scripts, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-1:] or [""]
            return r.returncode == 0, tail[0][:160]
        return False, "unknown check kind %r" % k
    except Exception as e:                                  # noqa: BLE001 - a broken check means "not done", never a crash
        return False, "check error: %s" % type(e).__name__


def is_duplicate(intents, candidate, now=None, cooldown=COOLDOWN_SEC):
    """True when an intent with the same dedupe key is still open or doing, or finished within the cooldown."""
    now = now_s() if now is None else now
    for i in intents:
        if i.get("dedupe") != candidate.get("dedupe"):
            continue
        if i.get("status") in ("open", "doing", "waiting"):
            return True
        if i.get("status") in ("done", "dropped") and now - float(i.get("closed_epoch") or i.get("epoch") or 0) < cooldown:
            return True
    return False


_OVERRIDES = {"mtime": None, "path": None, "data": {}}


def kind_demoted(kind, logs=None):
    """True when the learn step marked this KIND of intent shadow (acceptance under 50%, logs/intent_kind_overrides.json). Applies to intents raised AFTER the verdict too. The file is re-read only when it changes."""
    p = os.path.join(logs or _logs_dir(), "intent_kind_overrides.json")
    try:
        m = os.path.getmtime(p)
    except OSError:
        return False
    if _OVERRIDES["path"] != p or _OVERRIDES["mtime"] != m:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        _OVERRIDES.update(path=p, mtime=m, data=data if isinstance(data, dict) else {})
    return bool((_OVERRIDES["data"].get(kind) or {}).get("shadow"))


def actionable(intent, now=None):
    """True when this intent may be acted on right now without asking: open, not shadow, auto tier, no live lease, not_before passed."""
    now = now_s() if now is None else now
    if intent.get("status") != "open" or intent.get("shadow") or kind_demoted(intent.get("kind")):
        return False
    if int(intent.get("tier", 1)) not in AUTO_TIERS:
        return False
    nb = _parse_when(intent.get("not_before"))
    if nb is not None and now < nb:
        return False
    lease = intent.get("lease") or None
    return not (lease and float(lease.get("until", 0)) > now)


def _parse_when(value):
    """A date or date-time string as an epoch, or None when it is empty or unparseable."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(value, fmt))
        except ValueError:
            continue
    return None


def _deadline_epoch(intent):
    return _parse_when(intent.get("deadline"))


def rank(intents, now=None):
    """Actionable intents, most urgent first: a deadline inside 24 h wins, then higher value, then older."""
    now = now_s() if now is None else now
    out = [i for i in intents if actionable(i, now)]

    def key(i):
        de = _deadline_epoch(i)
        soon = 0 if (de is not None and de - now < 86400) else 1
        return (soon, de if (soon == 0 and de is not None) else 0, -int(i.get("value", 3)), float(i.get("epoch") or 0))
    return sorted(out, key=key)


def next_action(intents, now=None):
    """(intent or None, reason). The reason explains an empty queue, so a stop can be justified instead of guessed."""
    now = now_s() if now is None else now
    ranked = rank(intents, now)
    if ranked:
        return ranked[0], "top of %d actionable" % len(ranked)
    waiting = [i for i in intents if i.get("status") in ("open", "waiting") and int(i.get("tier", 1)) == 2]
    shadow = [i for i in intents if i.get("status") == "open" and i.get("shadow")]
    later = [i for i in intents if i.get("status") == "open" and (_parse_when(i.get("not_before")) or 0) > now]
    leased = [i for i in intents if i.get("status") in ("open", "doing") and (i.get("lease") or {}).get("until", 0) and float((i.get("lease") or {})["until"]) > now]
    bits = []
    if waiting:
        bits.append("%d waiting on the owner" % len(waiting))
    if leased:
        bits.append("%d already being worked" % len(leased))
    if shadow:
        bits.append("%d in shadow" % len(shadow))
    if later:
        bits.append("%d not due yet" % len(later))
    return None, ("nothing actionable: " + ", ".join(bits)) if bits else "nothing actionable: queue empty"


def expire_leases(intents, now=None):
    """Drop leases whose expiry has passed (a crashed run must not hold an intent forever). Returns the ids freed."""
    now = now_s() if now is None else now
    freed = []
    for i in intents:
        lease = i.get("lease")
        if lease and float(lease.get("until", 0)) <= now:
            i["lease"] = None
            if i.get("status") == "doing":
                i["status"] = "open"
            freed.append(i["id"])
    return freed


def apply_outcome(intent, result, evidence="", now=None):
    """Close an intent with its result. 'ok' and 'skipped' finish it; anything else reopens it once, then drops it."""
    now = now_s() if now is None else now
    intent["lease"] = None
    intent["closed_epoch"] = now
    intent["result"] = result
    intent["evidence"] = evidence
    if result in ("ok", "skipped"):
        intent["status"] = "done"
    else:
        tries = int(intent.get("tries") or 0) + 1
        intent["tries"] = tries
        intent["status"] = "open" if tries < 2 else "dropped"
        if intent["status"] == "open":
            intent.pop("closed_epoch", None)
    return intent


# ---------- storage (one json object per line; the whole intent list is small enough to rewrite) ----------
def path(name, logs=None):
    return os.path.join(logs or _logs_dir(), name)


class Read(list):
    """The intent list as read from disk, remembering what each intent looked like then (`base`), so write_intents can write back only what THIS reader changed."""
    base = None


def _canon(intent):
    return json.dumps(intent, sort_keys=True, ensure_ascii=False)


def _rows(name, logs=None, strict=False):
    """The rows of a jsonl file. A missing file is an empty list. Any other read error is retried a few times; if it persists a reader gets [] as before, but strict=True (every writer) raises instead, because a writer that mistook 'could not read' for 'empty' would drop the whole queue."""
    last = None
    for attempt in range(5):
        try:
            with open(path(name, logs), encoding="utf-8", errors="replace") as f:
                text = f.read()
        except FileNotFoundError:
            return []
        except OSError as e:
            last = e
            time.sleep(0.05 * (attempt + 1))
            continue
        out = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out
    if strict:
        raise last
    return []


def read_jsonl(name, logs=None):
    out = _rows(name, logs)
    if name != INTENTS:
        return out
    r = Read(out)
    r.base = {i.get("id"): _canon(i) for i in out if isinstance(i, dict)}
    return r


def merge_intents(mine, current):
    """Pure. Three-way merge at the level of one intent. `mine` is a Read (its .base is what the writer saw); `current` is what is on disk now. Intents the writer did not touch keep whatever is on disk; intents the writer added or changed replace the disk copy; intents the writer removed are removed."""
    base = getattr(mine, "base", None)
    if base is None:
        return list(mine)                                            # a list built by hand: keep the old rewrite-everything meaning
    cur = {i.get("id"): i for i in current if isinstance(i, dict)}
    order = [i.get("id") for i in current if isinstance(i, dict)]
    mine_by = {i.get("id"): i for i in mine if isinstance(i, dict)}
    for iid, i in mine_by.items():
        if iid not in base or _canon(i) != base[iid]:
            if iid not in cur:
                order.append(iid)
            cur[iid] = i
    for iid in base:
        if iid not in mine_by:
            cur.pop(iid, None)
    return [cur[x] for x in order if x in cur]


def _write_raw(intents, logs=None):
    tmp = path(INTENTS, logs) + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        for i in intents:
            f.write(json.dumps(i, ensure_ascii=False) + "\n")
    for attempt in range(6):                                          # a reader holding the file open makes os.replace raise PermissionError for a moment
        try:
            os.replace(tmp, path(INTENTS, logs))
            return
        except PermissionError:
            if attempt == 5:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(0.1 * (attempt + 1))


@contextlib.contextmanager
def _intents_lock(logs=None):
    """Hold the intents lock for the block. If it cannot be had in LOCK_WAIT seconds the block runs anyway: the loop must never stall on a lock.
    Uses a simple file-based lock using os.open with O_EXCL for cross-platform atomicity.
    """
    lg = logs or _logs_dir()
    lock_path = os.path.join(lg, ".intents.lock")
    got = False
    deadline = time.time() + LOCK_WAIT
    fd = None
    try:
        os.makedirs(lg, exist_ok=True)
        while not got:
            try:
                # O_CREAT | O_EXCL is atomic on both Unix and Windows
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                got = True
            except OSError:
                try:                                   # a holder that crashed leaves its lock file behind for ever: one older than LOCK_STALE is dead, remove it and try again at once
                    if time.time() - os.path.getmtime(lock_path) > LOCK_STALE:
                        os.remove(lock_path)
                        continue
                except OSError:
                    pass
                if time.time() >= deadline:
                    break
                time.sleep(0.05)
        yield
    finally:
        if got and fd is not None:
            try:
                os.close(fd)
                os.remove(lock_path)
            except OSError:
                pass


def write_intents(intents, logs=None):
    """Write the intent list back under a short lock, merging with what other actors wrote since it was read (see merge_intents). After the call the list holds the merged result, so a caller that carries on sees the others' changes too. Raises (and writes nothing) if the file cannot be read for the merge."""
    with _intents_lock(logs):
        merged = merge_intents(intents, _rows(INTENTS, logs, strict=True))
        _write_raw(merged, logs)
        if isinstance(intents, Read):
            intents[:] = merged
            intents.base = {i.get("id"): _canon(i) for i in merged if isinstance(i, dict)}


def claim(intent_id, by, secs=None, logs=None, now=None):
    """Compare-and-set claim, done under the lock on a fresh read. -> (code, message): 0 claimed, 2 no such intent, 5 refused (already closed, or someone else holds a live lease)."""
    now = now_s() if now is None else now
    secs = LEASE_SEC if secs is None else secs
    with _intents_lock(logs):
        rows = _rows(INTENTS, logs, strict=True)
        i = _one(rows, intent_id)
        if not i:
            return 2, "no such intent"
        if i.get("status") in ("done", "dropped"):
            return 5, "refused: %s is already %s" % (i["id"], i["status"])
        lease = i.get("lease")
        if lease and float(lease.get("until", 0)) > now and lease.get("by") != by:
            return 5, "refused: %s is held by %s for another %d s" % (i["id"], lease.get("by"), float(lease["until"]) - now)
        i["lease"] = {"by": by, "until": now + secs}
        i["status"] = "doing"
        _write_raw(rows, logs)
        return 0, "claimed %s by %s for %ds" % (i["id"], by, secs)


def append(name, row, logs=None):
    with open(path(name, logs), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _one(intents, intent_id):
    for i in intents:
        if i["id"] == intent_id or i["id"].startswith(intent_id):
            return i
    return None


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):       # an item can hold accents, dashes or emoji; a narrow pipe encoding (cmd, cp1252) must not turn the report into a crash
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="intent_store.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("add")
    a.add_argument("--kind", required=True)
    a.add_argument("--what", required=True)
    a.add_argument("--tier", type=int, default=1)
    a.add_argument("--value", type=int, default=3)
    a.add_argument("--dedupe")
    a.add_argument("--deadline")
    a.add_argument("--not-before")
    a.add_argument("--from-event")
    a.add_argument("--shadow", action="store_true")
    a.add_argument("--check", help="done-when test: pytest:test_x.py | dry_run:x.py | exists:PATH | contains:PATH::text")
    sub.add_parser("next")
    c = sub.add_parser("claim")
    c.add_argument("id")
    c.add_argument("--by", default="live-session")
    c.add_argument("--secs", type=int, default=LEASE_SEC)
    d = sub.add_parser("done")
    d.add_argument("id")
    d.add_argument("--result", default="ok")
    d.add_argument("--evidence", default="")
    l = sub.add_parser("list")
    l.add_argument("--all", action="store_true")
    args = p.parse_args(argv)
    intents = read_jsonl(INTENTS)
    freed = expire_leases(intents)
    if freed:
        write_intents(intents)
    if args.cmd == "add":
        check = parse_check(args.check) if args.check else None
        if args.check and not check:
            print("bad --check spec (use pytest:test_x.py | dry_run:x.py | exists:PATH | contains:PATH::text)")
            return 2
        cand = new_intent(args.kind, args.what, args.tier, args.value, args.dedupe, args.deadline, getattr(args, "from_event", None), args.shadow, not_before=getattr(args, "not_before", None), check=check)
        if is_duplicate(intents, cand):
            print("duplicate, not added (dedupe=%s)" % cand["dedupe"])
            return 0
        intents.append(cand)
        write_intents(intents)
        print("added %s tier%d value%d %s" % (cand["id"], cand["tier"], cand["value"], cand["what"]))
        return 0
    if args.cmd == "next":
        top, why = next_action(intents)
        print(("NEXT %s tier%d value%d %s" % (top["id"], top["tier"], top["value"], top["what"])) if top else "NONE " + why)
        return 0
    if args.cmd == "claim":
        code, msg = claim(args.id, args.by, args.secs)
        print(msg)
        return code
    if args.cmd == "done":
        i = _one(intents, args.id)
        if not i:
            print("no such intent")
            return 2
        if not (args.evidence or "").strip():
            print("NOT DONE: --evidence is required. Say what you saw that proves it (or why it was skipped or failed); an item closed without evidence is the same as one never done.")
            return 3
        if args.result == "ok" and i.get("check"):
            check = i["check"]
            ok, detail = run_check(check)
            intents = read_jsonl(INTENTS)               # the check can take up to two minutes: re-read so intents other actors added or closed meanwhile are not overwritten
            expire_leases(intents)
            i = _one(intents, args.id) or i
            if not ok:                                   # "repeat until perfect": the done-when check must pass before the intent can close as ok
                i["tries"] = int(i.get("tries", 0)) + 1
                write_intents(intents)
                print("NOT DONE (%s): the done-when check '%s' fails: %s. Fix it and close again." % (i["id"], describe_check(check), detail))
                return 6
            args.evidence = ("%s [done-when check passed: %s]" % (args.evidence, describe_check(check))).strip()
        apply_outcome(i, args.result, args.evidence)
        write_intents(intents)
        append(OUTCOMES, {"ts": iso(), "intent_id": i["id"], "action": i["what"], "evidence": args.evidence, "result": args.result, "reaction": None})
        print("closed %s as %s (status %s)" % (i["id"], args.result, i["status"]))
        return 0
    if args.cmd == "list":
        rows = intents if args.all else [i for i in intents if i.get("status") in ("open", "doing", "waiting")]
        for i in rows:
            print("%s %-14s t%d v%d %-9s %s%s" % (i["id"], i["kind"], i["tier"], i["value"], i["status"], "[shadow] " if i.get("shadow") else "", i["what"][:70]))
        print("%d of %d shown" % (len(rows), len(intents)))
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())