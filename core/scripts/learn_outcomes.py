# -*- coding: utf-8 -*-
"""Learn step: turns what the user said after each finished work item into a rate per kind of work item.

Why: perceive, decide, execute had code; learn did not. A work item that landed badly (the user corrected or nudged right after) looked exactly like one that landed well, so nothing
could improve by itself. For each work item closed ok, this reads the user's prompts in the next 30 minutes (logs/user_prompts.jsonl, written by prompt_log_hook.py) and labels it:
    correction  the user said it was wrong / a mistake / not what they asked       nudge  the user had to prod ("keep going", "you keep stopping")
    approval    a plain thanks / yes / nice                                        none   silence
Rules decide; nothing here calls a model. Then per kind of work item: acceptance = share of closed items that drew no correction or nudge.
    a kind under 50% after 5 closed   -> proposal: demote (mark the kind shadow: still logged, never acted on) + one row in the correction ledger
    a kind over 80% after 5 closed    -> proposal: promote (open items of that kind get their value raised by one, maximum 4)
Never changes a tier, never touches tier 2 or 3.

What it does on its own: appends labels to logs/outcome_labels.jsonl (append-only; outcomes.jsonl is never touched) and raises a tier-2 work item for each proposal ("do you want this
kind demoted?"). It changes nothing else until you opt in with --apply (or by creating logs/learn_outcomes_apply_on.txt): then demotions go to logs/intent_kind_overrides.json (which
intent_store.py already reads, so demoted kinds stop being picked up) and promotions raise the value of open items. Undo a demotion with --release KIND.
The correction ledger is vault/05 - Resources/Correction Ledger.md when that note exists, otherwise logs/correction_ledger.md.

Needs prompt_log_hook.py wired as a UserPromptSubmit hook: with no prompt log there is nothing to judge a reaction from, so nothing is labelled (a missing log must never read as "everyone is happy").
Outcomes closed before the first logged prompt are skipped for the same reason.
    python learn_outcomes.py [--dry-run] [--apply] [--release KIND]     # --dry-run prints the table and writes nothing
Kill switch: logs/learn_outcomes_off.txt (a dry run ignores it).
Pure core (unit tested): label(texts, intent_tokens), attach_labels(outcomes, intents, prompts, now), rates(labeled), proposals(rate_table, overrides).
"""
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).parent.resolve()
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import intent_store  # noqa: E402
import prompt_log_hook  # noqa: E402

ROOT = Path(os.environ.get("HARNESS_ROOT") or SCRIPTS.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

WINDOW = 30 * 60
MIN_CLOSED = 5
DEMOTE_BELOW, PROMOTE_ABOVE, MAX_VALUE = 0.5, 0.8, 4
LABELS = "outcome_labels.jsonl"
OVERRIDES = "intent_kind_overrides.json"
OFF = "learn_outcomes_off.txt"
APPLY_SWITCH = "learn_outcomes_apply_on.txt"
LAST = "learn_outcomes_last.txt"

# What counts as a correction, a nudge, an approval. A word like "again" alone is not a correction ("password again").
CORRECTION_RX = (r"mistake|wrong|forgot|assum|dropped|losing? focus|even after|\bnot what i (asked|meant|wanted)|\bi (didn.?t|did not) (ask|want)"
                 r"|\bstop (doing|sending)\b|\bwhy (did|are) (u|you)\b|\bundo (that|this|it)\b")
NUDGE_RX = (r"^\W*(continue|keep going|go on|carry on)\W*$|you keep stopping|u keep stopping|why (have|did|are) (u|you) (stop|stopp)|did (u|you) forget"
            r"|are (u|you) (still there|alive)|still (waiting|nothing)")
APPROVE_RX = (r"^\W*(yes|yep|yeah|ok|okay|nice|good|great|thanks|thank you|thx|perfect|awesome|cool|sweet|bet|exactly)\b"
              r"|\b(good job|well done|nice work|looks good|that works|love it)\b")
CORRECT = re.compile(CORRECTION_RX, re.I)
NUDGE = re.compile(NUDGE_RX, re.I)
APPROVE = re.compile(APPROVE_RX, re.I)

GENERIC = frozenset("harness intent intents script scripts sensor sensors tests there about which their would should could because still after before other these those thing things stuff"
                    " first again always never every where while being doing going making using notes vault files".split())
EXCLUDE_KINDS = frozenset(("canary", "budget_trip", "harness_red", "sensor_failing", "intent_kind_review"))     # internal plumbing: a reaction to these says nothing about whether the work was wanted


def get_root():
    return Path(os.environ.get("HARNESS_ROOT") or SCRIPTS.parent).resolve()


def get_logs():
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()


def get_vault():
    return Path(os.environ.get("HARNESS_VAULT") or get_root() / "vault").resolve()


def ledger_path():
    """The correction ledger note in the vault when it exists, otherwise logs/correction_ledger.md."""
    note = get_vault() / "05 - Resources" / "Correction Ledger.md"
    return note if note.exists() else get_logs() / "correction_ledger.md"


def tokens(text):
    """Pure. Content words (5+ letters, not generic) of a text, used to check that a correction is about THIS action and not about something said in the same half hour."""
    return {w for w in re.findall(r"[a-z]{5,}", (text or "").lower()) if w not in GENERIC}


def label(texts, intent_tokens=None):
    """Pure. The most significant label for the messages sent in the window: correction beats nudge beats approval beats none.
    With intent_tokens given, a correction only counts when it shares a content word with the action (tone corrections about other things must not be blamed on whatever had
    just been closed); a nudge ("keep going", "you keep stopping") is about stopping, so it always counts."""
    texts = [t or "" for t in texts or []]
    if any(CORRECT.search(t) and (intent_tokens is None or tokens(t) & intent_tokens) for t in texts):
        return "correction"
    if any(NUDGE.search(t) for t in texts):
        return "nudge"
    if any(APPROVE.search(t) for t in texts):
        return "approval"
    return "none"


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def attach_labels(outcomes, intents, prompts, now, already=(), feed_start=None):
    """Pure. Label every ok outcome whose 30-minute window is complete, that was not labelled before and that closed after the prompt log began (feed_start, epoch).
    prompts: [{ts (epoch or iso), text}] typed by the user. -> [{intent_id, kind, label, ts}]."""
    by_id = {i.get("id"): i for i in intents or [] if isinstance(i, dict)}
    seen = set(already or ())
    msgs = sorted(((_epoch(d.get("ts")), str(d.get("text", ""))) for d in prompts or [] if isinstance(d, dict)), key=lambda x: x[0] or 0)
    out = []
    for o in outcomes or []:
        if not isinstance(o, dict):
            continue
        iid, t = o.get("intent_id"), _epoch(o.get("ts"))
        if not iid or t is None or iid in seen or o.get("result") != "ok" or now - t < WINDOW:
            continue
        if feed_start is not None and t < feed_start:
            continue
        intent = by_id.get(iid) or {}
        kind = intent.get("kind")
        if not kind or kind in EXCLUDE_KINDS:
            continue
        window = [txt for (mt, txt) in msgs if mt is not None and t < mt <= t + WINDOW]
        mine = tokens(" ".join([str(intent.get("what", "")), str(o.get("action", "")), str(o.get("evidence", ""))]))
        out.append({"intent_id": iid, "kind": kind, "label": label(window, mine), "ts": t})
        seen.add(iid)
    return out


def rates(labeled):
    """Pure. -> {kind: (closed, accepted, rate)}. accepted = no correction and no nudge."""
    tally = {}
    for r in labeled or []:
        if not isinstance(r, dict) or not r.get("kind"):
            continue
        c, a = tally.get(r["kind"], (0, 0))
        tally[r["kind"]] = (c + 1, a + (1 if r.get("label") in ("approval", "none") else 0))
    return {k: (c, a, a / c) for k, (c, a) in tally.items()}


def proposals(rate_table, overrides):
    """Pure. -> [{kind, action, rate, closed}] with action demote | promote. Only kinds with at least MIN_CLOSED closed intents, and never one already applied."""
    out = []
    for kind, (closed, _acc, rate) in sorted((rate_table or {}).items()):
        if closed < MIN_CLOSED:
            continue
        cur = (overrides or {}).get(kind) or {}
        if rate < DEMOTE_BELOW and not cur.get("shadow"):
            out.append({"kind": kind, "action": "demote", "rate": rate, "closed": closed})
        elif rate > PROMOTE_ABOVE and cur.get("bonus", 0) < 1:
            out.append({"kind": kind, "action": "promote", "rate": rate, "closed": closed})
    return out


def describe(p):
    pct = round(p["rate"] * 100)
    if p["action"] == "demote":
        return "work items of kind %s were accepted %d%% of the time over %d closed; proposal: stop acting on that kind until it is reviewed" % (p["kind"], pct, p["closed"])
    return "work items of kind %s were accepted %d%% of the time over %d closed; proposal: raise the value of open items of that kind by one" % (p["kind"], pct, p["closed"])


def _load_overrides(logs=None):
    try:
        with open(Path(logs or get_logs()) / OVERRIDES, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_overrides(logs, data):
    p = Path(logs) / OVERRIDES
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)


LEDGER_HEADER = "# Correction Ledger\n\nRows written by learn_outcomes.py when a kind of work item is demoted.\n\n| when | what happened | measured | why | what changed |\n|---|---|---|---|---|\n"


def _ledger_row(p):
    return "| %s (auto) | work items of kind %s drew a correction or nudge on %d of %d closed | acceptance %d%%, under the 50%% line | an unrequested kind of action the user does not want yet [intent-demotion] | marked shadow until reviewed: still logged, never acted on |\n" % (
        time.strftime("%m-%d %H:%M"), p["kind"], round(p["closed"] * (1 - p["rate"])), p["closed"], round(p["rate"] * 100))


def _append_ledger(path, row):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        with open(path, "a", encoding="utf-8") as f:
            if fresh:
                f.write(LEDGER_HEADER)
            f.write(row)
        return True
    except OSError:
        return False


def apply(props, logs=None, ledger=None):
    """Write the verdicts. A demotion goes into the overrides file (read by intent_store.actionable) and one ledger row; a promotion raises the value of the open intents of that kind
    (tier 0 or 1 only, maximum 4). -> the overrides dict."""
    logs = str(logs or get_logs())
    ov = _load_overrides(logs)
    intents = intent_store.read_jsonl(intent_store.INTENTS, logs)
    bumped = False
    for p in props:
        entry = ov.setdefault(p["kind"], {})
        if p["action"] == "demote":
            entry.update(shadow=True, rate=round(p["rate"], 2), closed=p["closed"], since=time.time())
            _append_ledger(ledger or ledger_path(), _ledger_row(p))
        else:
            for i in intents:
                if i.get("kind") == p["kind"] and i.get("status") in ("open", "waiting") and i.get("tier") in intent_store.AUTO_TIERS and i.get("value", 0) < MAX_VALUE:
                    i["value"] = i["value"] + 1
                    bumped = True
            entry.update(bonus=1, rate=round(p["rate"], 2), closed=p["closed"], since=time.time())
    if props:
        if bumped:
            intent_store.write_intents(intents, logs)
        _save_overrides(logs, ov)
    return ov


def release(kind, logs=None):
    """Undo a demotion: remove the kind from the overrides file. -> True when there was an entry."""
    logs = str(logs or get_logs())
    ov = _load_overrides(logs)
    if kind not in ov:
        return False
    ov.pop(kind)
    _save_overrides(logs, ov)
    return True


def _touch_last(logs):
    try:
        p = Path(logs) / LAST
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(time.strftime("%F %T") + "\n", encoding="utf-8")
    except OSError:
        pass


def _announce(props, applied, logs):
    try:
        import notify
    except Exception:
        return
    for p in props:
        if applied:
            notify.alert("applied: " + describe(p), source="learn_outcomes", logs=logs)
        else:
            notify.alert(describe(p) + " (nothing changed; run learn_outcomes.py --apply to do it)", source="learn_outcomes", intent_kind="intent_kind_review", tier=2, value=3,
                         dedupe="learn-%s-%s" % (p["action"], p["kind"]), logs=logs)


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logs = str(get_logs())
    dry = "--dry-run" in argv
    if "--release" in argv:
        i = argv.index("--release")
        kind = argv[i + 1] if i + 1 < len(argv) else ""
        if not kind:
            print("learn_outcomes: --release needs a kind name")
            return 2
        print("learn_outcomes: released %s" % kind if release(kind, logs) else "learn_outcomes: %s has no override" % kind)
        return 0
    if not dry and (get_logs() / OFF).exists():
        print("learn_outcomes: off")
        return 0
    prompts = prompt_log_hook.read_prompts(logs)
    if not prompts:
        print("learn_outcomes: no prompt log yet (logs/user_prompts.jsonl is missing or empty; prompt_log_hook.py writes it), so there is nothing to learn from")
        if not dry:
            _touch_last(logs)
        return 0
    outcomes = intent_store.read_jsonl(intent_store.OUTCOMES, logs)
    intents = intent_store.read_jsonl(intent_store.INTENTS, logs)
    previous = intent_store.read_jsonl(LABELS, logs)
    new = attach_labels(outcomes, intents, prompts, time.time(), already={r.get("intent_id") for r in previous if isinstance(r, dict)}, feed_start=prompts[0]["ts"])
    table = rates(previous + new)
    props = proposals(table, _load_overrides(logs))
    for kind, (c, a, r) in sorted(table.items(), key=lambda kv: -kv[1][0]):
        print("%-28s closed %3d  accepted %3d  rate %3d%%" % (kind, c, a, round(r * 100)))
    print("%d new label(s); %d proposal(s): %s" % (len(new), len(props), ", ".join("%s %s" % (p["action"], p["kind"]) for p in props) or "none"))
    if dry:
        return 0
    for r in new:
        intent_store.append(LABELS, r, logs)
    do_apply = "--apply" in argv or (get_logs() / APPLY_SWITCH).exists()
    if do_apply:
        apply(props, logs)
    _announce(props, do_apply, logs)
    _touch_last(logs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
