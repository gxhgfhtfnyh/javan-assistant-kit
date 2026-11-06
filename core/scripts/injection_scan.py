# -*- coding: utf-8 -*-
"""Injection scan: looks through every note in the vault for text that tries to give orders to an AI.

Notes are read by the assistant, so a note can carry a payload: phrasing meant to make a later session treat the note's text as instructions
("ignore previous instructions", "you are now ...", "do not tell the user"), invisible characters that hide text from a person skimming the note while a model
still reads it (zero-width characters, right-to-left overrides, invisible tag characters), and big base64 blobs that have no business in a plain note.
This is the at-rest layer: it catches a payload sitting in a file before any session opens it. (The incoming-content hook, injection_prescan_hook.py, covers
what a web tool brings back.) It is pattern based on purpose: fast, free and safe to run as often as you like. It does not delete or change anything.

    python injection_scan.py             # scan the vault, log the result, alert on NEW findings, exit 1 if there are any
    python injection_scan.py --dry-run   # scan and print only: no log, no alert
    python injection_scan.py --job       # same as the first, but always exit 0 (a timed job must not look broken when it finds something)
Every run appends one row to logs/injection_scan.jsonl: {timestamp, scanned, flagged_files, new_files, findings: {note: [finding, ...]}}.
A finding is {type: injection_phrase, line, match} | {type: hidden_unicode, count} | {type: large_base64_blob, line, length} | {type: oversize, bytes} (a note over 4 MB
is only scanned up to there, so it is flagged for a person to look at).
Findings you have reviewed and accepted (python injection_known.py --accept) are not alerted again: only new ones become a line in logs/alerts.txt and a work item
(kind injection_in_note, tier 2 = ask the owner, value 3), once each. The logged row still lists everything.
Ignored folders: .obsidian, .git, .trash and 04 - Archive. Scanned: .md, .markdown and .txt notes.
Your assistant's name: a sentence like "you are now <your assistant's name>" in a persona note is normal, so names listed in the environment variable
HARNESS_ASSISTANT_NAME (comma separated) or in logs/injection_scan_names.txt (one per line) are not flagged after "you are now".
Kill switch: logs/injection_scan_off.txt. Environment: HARNESS_ROOT, HARNESS_LOGS, HARNESS_VAULT.
Known limits: a legitimate note ABOUT prompt injection, a pasted image data URI, an emoji joined with the zero-width joiner between ASCII letters and a Persian
zero-width non-joiner are flagged too; review them once and accept them as the baseline.
Pure core (unit tested): injection_re, scan_text, scan_file, scan_vault."""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).parent.resolve()
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

SOURCE = "injection_scan"
OFF_NAME = "injection_scan_off.txt"
LOG_NAME = "injection_scan.jsonl"
STATE_NAME = "injection_scan_state.json"
NAMES_NAME = "injection_scan_names.txt"
IGNORE_DIRS = {".obsidian", ".git", ".trash", "04 - Archive"}
SUFFIXES = {".md", ".markdown", ".txt"}
MAX_BYTES = 4 * 1024 * 1024
MAX_FILES = 50000
MAX_ALERTS_PER_RUN = 5

INJECTION_PHRASES = [
    r"ignore (all )?(previous|prior|above) instructions",
    r"disregard (all )?(previous|prior|above)",
    r"you are now (in )?{NAME_GUARD}",
    r"new system prompt",
    r"\bsystem\s*:\s*you (are|must|will)",
    r"\bassistant\s*:\s*(sure|okay|i will)",
    r"do not (tell|inform|mention this to) (the user|the owner|your owner|your user)",
    r"this is not a test.{0,30}(execute|run|do this)",
    r"jailbreak",
    r"pretend (you have no|you don't have) (restrictions|rules|guidelines)",
]

# Invisible or direction-changing characters: zero-width space/joiners, direction marks and overrides, word joiner and invisible operators,
# bidi isolates, soft hyphen, mongolian vowel separator, byte order mark, and the invisible "tag" block.
HIDDEN_UNICODE_RE = re.compile("[­᠎​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")
BASE64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")


def injection_re(names=()):
    """The compiled phrase pattern. `names` are assistant names that may follow 'you are now' without being flagged."""
    names = [n for n in (str(x).strip() for x in names) if n]
    guard = ("(?!%s)" % "|".join(re.escape(n) for n in names)) if names else ""
    return re.compile("|".join(p.replace("{NAME_GUARD}", guard) for p in INJECTION_PHRASES), re.IGNORECASE)


INJECTION_RE = injection_re()


def _env_path(name):
    value = os.environ.get(name)
    return Path(value).resolve() if value else None


def root():
    return _env_path("HARNESS_ROOT") or ROOT


def logs_dir():
    return _env_path("HARNESS_LOGS") or ((root() / "logs") if os.environ.get("HARNESS_ROOT") else LOGS)


def vault_dir():
    return _env_path("HARNESS_VAULT") or ((root() / "vault") if os.environ.get("HARNESS_ROOT") else VAULT)


def assistant_names():
    names = [n for n in os.environ.get("HARNESS_ASSISTANT_NAME", "").split(",") if n.strip()]
    try:
        names += [l.strip() for l in (logs_dir() / NAMES_NAME).read_text(encoding="utf-8", errors="replace").splitlines()[:50] if l.strip() and not l.startswith("#")]
    except OSError:
        pass
    return names


def _hidden_chars(text):
    """The hidden characters in `text`, except a joiner inside an emoji sequence (flanked by symbols), which is how emoji are built."""
    found = []
    for m in HIDDEN_UNICODE_RE.finditer(text):
        ch = m.group(0)
        if ch == "‍":
            before = text[m.start() - 1] if m.start() > 0 else ""
            if before == "️" and m.start() > 1:
                before = text[m.start() - 2]
            after = text[m.end()] if m.end() < len(text) else ""
            if before and after and ord(before) >= 0x2190 and ord(after) >= 0x2190:
                continue
        found.append(ch)
    return found


def scan_text(text, regex=None):
    """-> list of finding dicts for one note's text. A single byte order mark at the very start is ordinary (many editors write one) and is ignored."""
    regex = regex or INJECTION_RE
    if text.startswith("﻿"):
        text = text[1:]
    findings = []
    for m in regex.finditer(text):
        findings.append({"type": "injection_phrase", "line": text.count("\n", 0, m.start()) + 1, "match": " ".join(m.group(0).split())[:80]})
    hidden = _hidden_chars(text)
    if hidden:
        findings.append({"type": "hidden_unicode", "count": len(hidden)})
    for m in BASE64_BLOB_RE.finditer(text):
        findings.append({"type": "large_base64_blob", "line": text.count("\n", 0, m.start()) + 1, "length": len(m.group(0))})
    return findings


def scan_file(path, regex=None, max_bytes=MAX_BYTES):
    """-> findings for one file. An unreadable file gives []. A file over `max_bytes` is scanned up to there and gets an 'oversize' finding."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            raw = f.read(max_bytes)
    except OSError:
        return []
    findings = scan_text(raw.decode("utf-8", errors="ignore"), regex)
    if size > max_bytes:
        findings.append({"type": "oversize", "bytes": size})
    return findings


def vault_notes(vault=None):
    """Every note to scan, as (path, posix path relative to the vault), in a stable order."""
    vault = Path(vault or vault_dir())
    out = []
    for dirpath, dirnames, filenames in os.walk(vault, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in SUFFIXES:
                continue
            p = Path(dirpath) / name
            if p.is_symlink():
                continue
            out.append((p, p.relative_to(vault).as_posix()))
            if len(out) >= MAX_FILES:
                return out
    return out


def scan_vault(vault=None, regex=None):
    """-> {timestamp, scanned, flagged_files, findings: {relative note: [finding, ...]}}. Writes nothing."""
    findings = {}
    notes = vault_notes(vault)
    for p, rel in notes:
        got = scan_file(p, regex)
        if got:
            findings[rel] = got
    return {"timestamp": dt.datetime.now().isoformat(timespec="seconds"), "scanned": len(notes), "flagged_files": len(findings), "findings": findings}


def _known():
    """The injection_known module, or None when it cannot be imported (then every finding counts as new)."""
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import injection_known
        return injection_known
    except Exception:
        return None


def new_findings_of(findings):
    """{note: [[type, detail], ...]} of the findings not in the reviewed baseline. An unreadable baseline counts as no baseline (nothing is hidden)."""
    known = _known()
    if known is None:
        return {n: [[f.get("type", ""), ""] for f in v] for n, v in findings.items()}
    current = known.signatures(findings)
    try:
        baseline, _problem = known.load_baseline(logs_dir())
    except Exception:
        baseline = None
    return known.new_findings(current, baseline or {})


def _load_state():
    try:
        data = json.loads((logs_dir() / STATE_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        lg = logs_dir()
        lg.mkdir(parents=True, exist_ok=True)
        tmp = lg / (STATE_NAME + ".tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, lg / STATE_NAME)
    except OSError:
        pass


def _alert(text, dedupe):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source=SOURCE, intent_kind="injection_in_note", tier=2, value=3, dedupe=dedupe, toast=True, logs=str(logs_dir()))
        return
    except Exception:
        pass
    try:
        lg = logs_dir()
        lg.mkdir(parents=True, exist_ok=True)
        with (lg / "alerts.txt").open("a", encoding="utf-8") as f:
            f.write("[%s] %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), SOURCE, " ".join(text.split())[:500]))
    except OSError:
        pass


def check(dry_run=False):
    """Scan, log, alert on new findings. -> the scan result plus new_files (a mapping note -> new signatures)."""
    result = scan_vault(regex=injection_re(assistant_names()))
    new = new_findings_of(result["findings"])
    result["new_files"] = new
    if dry_run:
        return result
    lg = logs_dir()
    try:
        lg.mkdir(parents=True, exist_ok=True)
        with (lg / LOG_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps({**result, "new_files": len(new)}) + "\n")
    except OSError:
        pass
    if new:
        state = _load_state()
        alerted = list(state.get("alerted", []))
        fresh = []
        for name in sorted(new):
            key = name + ":" + hashlib.sha1(json.dumps(new[name], sort_keys=True).encode("utf-8")).hexdigest()[:10]
            if key not in alerted:
                fresh.append((name, key))
        for name, key in fresh[:MAX_ALERTS_PER_RUN]:
            kinds = ", ".join(sorted({t for t, _d in new[name]}))
            _alert("suspicious content in note %s (%s). Open it as data, not as instructions; if it is harmless run injection_known.py --accept." % (name, kinds), "injection:" + key)
            alerted.append(key)
        if len(fresh) > MAX_ALERTS_PER_RUN:
            _alert("%d more notes with new injection-scan findings; run injection_scan.py to list them." % (len(fresh) - MAX_ALERTS_PER_RUN), "injection-more:" + dt.date.today().isoformat())
            alerted.extend(k for _n, k in fresh[MAX_ALERTS_PER_RUN:])
        state["alerted"] = alerted[-500:]
        _save_state(state)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan the vault for injection phrases, hidden characters and base64 blobs.")
    ap.add_argument("--dry-run", action="store_true", help="print only: no log row, no alert, no state")
    ap.add_argument("--job", action="store_true", help="timed run: always exit 0")
    args = ap.parse_args(argv)
    if (logs_dir() / OFF_NAME).exists():
        print("injection_scan: off (logs/%s exists)" % OFF_NAME)
        return 0
    if not vault_dir().is_dir():
        print("injection_scan: no vault folder at %s, nothing to scan" % vault_dir())
        return 0
    r = check(dry_run=args.dry_run)
    new = r["new_files"]
    if new:
        print("[FLAGGED] %d note(s) with new findings, %d flagged in total, out of %d scanned:" % (len(new), r["flagged_files"], r["scanned"]))
        for name, sigs in sorted(new.items()):
            print("  %s: %s" % (name, sigs))
    elif r["flagged_files"]:
        print("[OK] %d note(s) scanned; %d flagged but all already reviewed" % (r["scanned"], r["flagged_files"]))
    else:
        print("[OK] %d vault notes scanned, nothing suspicious" % r["scanned"])
    return 0 if args.job or not new else 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
