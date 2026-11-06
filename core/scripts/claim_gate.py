# -*- coding: utf-8 -*-
"""Claim gate for Claude Code, LOG-ONLY. It never blocks and never prints.

Stop hook. Reads the hook JSON from stdin (field: transcript_path), finds the last turn in
that transcript, and records whether the final reply claims "done / fixed / saved / verified"
and what tool evidence the turn actually had.

Output: logs/claim_gate.jsonl, one line per turn that has claim words, an optional offer
("want me to ...?") or a refusal ("I can't ..."):
    {ts, claims, offers, refusals, n_tool_uses, n_errors, flagged_no_tools, flagged_errors, gaps, text_head}

Evidence gaps (deterministic checks of "I did X" against the turn's tool calls):
    write  "I saved / updated / wrote ..." with no Write/Edit call and no file-writing shell command
    tests  "all tests pass" with no shell command that mentions a test
    log    "logged ..." with no shell command that mentions a log or appends to a file, and no Write/Edit

Refusals: each sentence where the reply says it cannot do something becomes a tier-1, value-5
intent of kind capability_gap in the intent store (see intent_store.py), so the stop gate holds
the turn open until the ladder is climbed: (1) search what exists, (2) find a tool, (3) write a
script with tests, (4) only then one precise ask.

Kill switches (files, no restart needed):
    logs/claim_gate_off.txt      the whole hook does nothing
    logs/refusal_watch_off.txt   claims are still logged, but refusals raise no intents

Limits: regex claim detection misses phrasings and cannot tell whether a tool result really
proves the claim. Treat the log as a baseline to look at, not a verdict.

Hook config (settings.json):
    {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python core/scripts/claim_gate.py"}]}]}}

Pure, unit-tested core: claimed, refusals, gap_intent, evidence_gaps, analyze. Run: python claim_gate.py
"""
import collections
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

OUT_FILE = LOGS / "claim_gate.jsonl"
OFF_FILE = LOGS / "claim_gate_off.txt"
REFUSAL_OFF_NAME = "refusal_watch_off.txt"
TAIL_LINES = 600                 # only the end of a long transcript matters: the last turn
MAX_REFUSALS = 3
GAP_VALUE = 5

CLAIM = re.compile(r"\b(fixed|saved|logged|added|wrote|written|installed|updated|created|applied|done|verified|tested|confirmed|works|working now|built|corrected)\b", re.I)
OFFER = re.compile(r"\b(want me to|should i|shall i|do you want me|would you like me|let me know if you(?:'d| would)? like|happy to (?:also )?(?:do|build|write|run))\b", re.I)

# "I saved ..." / "I've updated ..." or a sentence that starts with the verb ("Saved the notes.").
# The named group v is the verb, so claimed() looks at the words in front of the verb, not at the full stop before it.
CLAIM_WRITE = re.compile(r"(?:\bI(?:'ve| have)? |(?:^|[.!?]\s+|\n\s*))(?P<v>saved|wrote|updated|added|created|appended|edited|rewrote|banked)\b", re.I)
CLAIM_TESTS = re.compile(r"\b(\d+ )?tests? (all )?pass(ed|es)?\b|\ball \d+ tests\b", re.I)
CLAIM_LOG = re.compile(r"\blogged\b", re.I)
WRITE_BASH = re.compile(r"(>>?\s*\S|\bsed -i\b|\btee\b|\bopen\([^)]*[\"']w|\bwrite_text\b|\bSet-Content\b|\bAdd-Content\b|\bOut-File\b|\bcp\b|\bmv\b)", re.I)
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "bash", "PowerShell"}

REFUSAL = re.compile(
    r"\bI(?:[’']m| am) (?:not able|unable) to\b"
    r"|\bI (?:can[’']?t|cannot|can not) (?:actually )?(?:do|access|open|read|send|log ?in|click|run|reach|install|set ?up|fix|connect|create|write|watch|listen|call|pay|buy|book|retrieve|view|edit|generate|download|upload|browse|track|monitor|hear|play|execute|modify|delete|share|post|schedule|control|stream|record|print|translate)\b"
    r"|\bI (?:don[’']?t|do not) have (?:access|a way|the ability|permission|credentials|an? (?:api|account|token|key))\b"
    r"|\bI have no way to\b|\bthere(?:'s| is) no way for me to\b|\bnot possible for me\b", re.I)

NEGATED = re.compile(r"\b(?:haven'?t|hasn'?t|have not|has not|did not|didn'?t|do not|don'?t|never|without)\b[^.!?,;\n]{0,40}$", re.I)
FUTURE = re.compile(r"\b(?:will|would|should|shall|going to|to be|need(?:s)? to|gets|once|until|when|if|next)\b[^.!?,;\n]{0,40}$", re.I)

GAP_TEXT = ("CAPABILITY GAP: I told the user \"%s\". Climb the ladder before refusing: (1) search what exists (the scripts folder, the toolbox notes, installed skills); "
            "(2) find a tool, MCP server, CLI or library that does it; (3) write a script with tests and register it; (4) only if it needs a password, a physical step "
            "or the user's decision, file ONE precise ask with the exact action. Close this intent with the evidence (what now works, or the one precise ask).")


# ---------- pure core ----------
def claimed(rx, text):
    """True when `rx` matches a sentence that CLAIMS the action, not one that denies it or only promises it.
    'I haven't logged it' is negated; 'once the tests pass' and 'I will have saved it' are plans. The 60 characters in front of the
    match (stopping at the nearest sentence end) are checked for a negation or a future marker. If `rx` has a group named v, the check
    starts at that group, so a full stop in front of the verb does not hide the words before it."""
    text = text or ""
    for m in rx.finditer(text):
        start = m.start("v") if "v" in rx.groupindex else m.start()
        before = text[max(0, start - 60):start]
        if not NEGATED.search(before) and not FUTURE.search(before):
            return True
    return False


def refusals(text):
    """Sentences where the reply says it cannot do something. Each one is a capability gap to climb out of, not an answer.
    Up to MAX_REFUSALS, each cut to 200 characters. A reply that starts with [no-reply] is private working text and is skipped."""
    t = (text or "").strip()
    if not t or t.startswith("[no-reply]"):
        return []
    out = []
    for s in re.split(r"(?<=[.!?])\s+|\n+", t):
        if REFUSAL.search(s):
            out.append(" ".join(s.split())[:200])
    return out[:MAX_REFUSALS]


def gap_intent(sentence, day=None):
    """-> (what, dedupe) for the intent a refusal raises. The same sentence on the same day is one intent (case and spacing ignored)."""
    key = hashlib.sha1(" ".join((sentence or "").lower().split()).encode("utf-8", "replace")).hexdigest()[:10]
    return GAP_TEXT % (sentence or "")[:160], "refusal-%s-%s" % (key, day or time.strftime("%Y%m%d"))


def _cmd(inp):
    return str(inp.get("command", "")) if isinstance(inp, dict) else ""


def evidence_gaps(text, calls):
    """Names of 'I did X' claims in `text` that have no matching call in `calls` [(tool name, tool input), ...]: write, tests, log.
    No tool call at all returns [] because flagged_no_tools already covers that; descriptive text is not a claim."""
    text = text or ""
    if not calls:
        return []
    cmds = [_cmd(i) for n, i in calls if n in SHELL_TOOLS]
    names = {n for n, _i in calls}
    gaps = []
    if claimed(CLAIM_WRITE, text) and not (names & WRITE_TOOLS) and not any(WRITE_BASH.search(c) for c in cmds):
        gaps.append("write")
    if claimed(CLAIM_TESTS, text) and not any("test" in c.lower() for c in cmds):
        gaps.append("tests")
    if claimed(CLAIM_LOG, text) and not any((">>" in c or "log" in c.lower()) for c in cmds) and not (names & WRITE_TOOLS):
        gaps.append("log")
    return gaps


def _content(row):
    m = row.get("message") if isinstance(row, dict) else None
    return m.get("content") if isinstance(m, dict) else None


def blocks(row):
    c = _content(row)
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def analyze(rows):
    """The claim record for the last turn in `rows` (transcript rows as dicts), or None when the final reply has no claim word, offer or refusal."""
    rows = [r for r in rows if isinstance(r, dict) and r.get("type") in ("user", "assistant")]
    start = 0
    for i, r in enumerate(rows):
        content = _content(r)
        # live transcripts store a user prompt as a plain string; list content with a text block counts too
        if r["type"] == "user" and ((isinstance(content, str) and content.strip()) or any(b.get("type") == "text" for b in blocks(r))):
            start = i
    n_tool = 0
    last_text = ""
    names, last_ok = {}, {}    # tool_use id -> tool name; tool name -> did its LAST result in this turn succeed
    calls = []
    for r in rows[start:]:
        for b in blocks(r):
            if r["type"] == "assistant" and b.get("type") == "tool_use":
                n_tool += 1
                names[b.get("id")] = b.get("name")
                calls.append((b.get("name"), b.get("input") or {}))
            elif r["type"] == "assistant" and b.get("type") == "text":
                last_text = str(b.get("text") or "")
            elif r["type"] == "user" and b.get("type") == "tool_result":
                last_ok[names.get(b.get("tool_use_id"), "?")] = not b.get("is_error")
    # An error that a later call of the same tool resolved (a retry after a rejected edit) is not a problem:
    # count only tools whose last result in the turn was still an error.
    n_err = sum(1 for ok in last_ok.values() if not ok)
    claims = sorted(set(m.lower() for m in CLAIM.findall(last_text)))
    offers = len(OFFER.findall(last_text[-600:]))          # optional-permission offers near the end of the reply
    refused = refusals(last_text)
    if not claims and not offers and not refused:
        return None
    return {"claims": claims, "offers": offers, "refusals": refused, "n_tool_uses": n_tool, "n_errors": n_err, "flagged_no_tools": bool(claims) and n_tool == 0,
            "flagged_errors": bool(claims) and n_err > 0, "gaps": evidence_gaps(last_text, calls), "text_head": last_text[:120].replace("\n", " ")}


# ---------- io ----------
def read_stdin():
    """The hook JSON from stdin, read as UTF-8 bytes (a console code page raised on emoji and the hook silently did nothing). {} on any problem."""
    try:
        buf = getattr(sys.stdin, "buffer", None)
        raw = buf.read().decode("utf-8", errors="replace") if buf is not None else sys.stdin.read()
        data = json.loads(raw) if (raw or "").strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_rows(path, tail=TAIL_LINES):
    """The last `tail` lines of a transcript parsed as JSON; lines that do not parse are skipped. Raises OSError when the file cannot be opened."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = collections.deque(f, maxlen=tail)
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def raise_gaps(sentences, logs=None):
    """Each refusal sentence becomes a tier-1, value-5 capability_gap intent (deduped per sentence per day). -> number of intents added."""
    lg = Path(logs) if logs else LOGS
    if not sentences or (lg / REFUSAL_OFF_NAME).exists():
        return 0
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import intent_store
    lg.mkdir(parents=True, exist_ok=True)
    intents = intent_store.read_jsonl(intent_store.INTENTS, str(lg))
    added = 0
    for s in sentences:
        what, dedupe = gap_intent(s)
        cand = intent_store.new_intent("capability_gap", what, tier=1, value=GAP_VALUE, dedupe=dedupe, from_event="refusal")
        if not intent_store.is_duplicate(intents, cand):
            intents.append(cand)
            added += 1
    if added:
        intent_store.write_intents(intents, str(lg))
    return added


def main():
    """Entry point. Never raises, never prints, never blocks the turn."""
    try:
        if OFF_FILE.exists():
            return
        path = read_stdin().get("transcript_path")
        if not path:
            return
        rec = analyze(load_rows(path))
        if rec is None:
            return
        rec = dict({"ts": time.strftime("%Y-%m-%d %H:%M:%S")}, **rec)
        try:
            OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
            with OUT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        try:
            raise_gaps(rec.get("refusals") or [])
        except Exception:
            pass
    except Exception:
        return


if __name__ == "__main__":
    main()
