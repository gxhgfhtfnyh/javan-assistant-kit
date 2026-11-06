# -*- coding: utf-8 -*-
"""Known-findings filter for the vault injection scan: tells you only what is NEW since you last reviewed the findings.

injection_scan.py flags notes that contain injection-style phrasing, hidden characters or big base64 blobs. Some of those are harmless (a note ABOUT prompt
injection quotes the phrases). Once you have looked at them, accept them as the baseline; from then on only a finding that is not in the baseline counts.

    python injection_known.py            # compare the latest scan (logs/injection_scan.jsonl) with the baseline (logs/injection_baseline.json)
    python injection_known.py --accept   # you reviewed the latest scan: make its findings the baseline (the only command that writes the baseline)
Exit 0: nothing new.  Exit 1: new findings, printed (the matched phrases, cut to 600 characters).  Exit 2: it cannot compare (no scan yet, or an unreadable file).
With no baseline yet every finding is new, so a first run never hides anything. A finding is identified by file, kind and the matched text, not by line number,
so editing a note above a reviewed phrase does not make it new again. Run injection_scan.py first (it also calls this module to alert only on new findings).
Environment: HARNESS_ROOT, HARNESS_LOGS. Pure core (unit tested): finding_detail, signatures, new_findings, latest_scan, load_baseline."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

SCAN_LOG = "injection_scan.jsonl"
BASELINE = "injection_baseline.json"
TAIL_BYTES = 8 * 1024 * 1024         # only the end of the scan log is read: the newest row is last
MAX_PRINT = 600


def logs_dir():
    value = os.environ.get("HARNESS_LOGS")
    if value:
        return Path(value).resolve()
    if os.environ.get("HARNESS_ROOT"):
        return Path(os.environ["HARNESS_ROOT"]).resolve() / "logs"
    return LOGS


def finding_detail(finding):
    """What identifies a finding apart from its kind: the matched text, else the hidden-character count, else the blob length, else the size."""
    for key in ("match", "count", "length", "bytes"):
        if finding.get(key) is not None:
            return str(finding[key]).lower() if key == "match" else str(finding[key])
    return ""


def signatures(findings):
    """{file: [[type, detail], ...]} (sorted, no duplicates, no line numbers) from a scan row's `findings` mapping. Malformed entries are skipped."""
    out = {}
    for name, items in (findings or {}).items():
        sigs = set()
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("type"):
                sigs.add((str(item["type"]), finding_detail(item)))
        if sigs:
            out[str(name)] = [list(s) for s in sorted(sigs)]
    return out


def new_findings(current, baseline):
    """The signatures in `current` that are not in `baseline` (both as returned by signatures): {file: [[type, detail], ...]}."""
    known = {f: {tuple(t) for t in v} for f, v in (baseline or {}).items()}
    out = {}
    for name, sigs in (current or {}).items():
        fresh = [s for s in sigs if tuple(s) not in known.get(name, set())]
        if fresh:
            out[name] = fresh
    return out


def latest_scan(logs=None):
    """The newest row of the scan log as a dict. -> (row or None, problem or None). A missing, empty or unparseable log gives (None, why)."""
    path = Path(logs or logs_dir()) / SCAN_LOG
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
            data = f.read()
    except FileNotFoundError:
        return None, "no scan log yet (%s); run injection_scan.py first" % SCAN_LOG
    except OSError as e:
        return None, "cannot read %s (%s)" % (SCAN_LOG, type(e).__name__)
    lines = [l for l in data.decode("utf-8", errors="replace").splitlines() if l.strip()]
    if not lines:
        return None, "the scan log %s is empty; run injection_scan.py first" % SCAN_LOG
    try:
        row = json.loads(lines[-1])
    except ValueError:
        return None, "the last row of %s is not valid JSON" % SCAN_LOG
    if not isinstance(row, dict):
        return None, "the last row of %s is not a scan result" % SCAN_LOG
    return row, None


def load_baseline(logs=None):
    """-> (signatures or None, problem or None). No baseline file is (None, None); an unreadable one is (None, why) so nothing is silently treated as reviewed."""
    path = Path(logs or logs_dir()) / BASELINE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:
        return None, "the baseline %s cannot be read (%s)" % (BASELINE, type(e).__name__)
    if not isinstance(data, dict):
        return None, "the baseline %s is not a mapping" % BASELINE
    out = {}
    for name, items in data.items():
        if isinstance(items, list):
            out[str(name)] = [list(t) for t in items if isinstance(t, (list, tuple)) and len(t) == 2]
    return out, None


def save_baseline(sigs, logs=None):
    path = Path(logs or logs_dir()) / BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(sigs, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Show only injection-scan findings that are new since the last review.")
    ap.add_argument("--accept", action="store_true", help="make the latest scan's findings the reviewed baseline")
    args = ap.parse_args(argv)
    row, problem = latest_scan()
    if problem:
        print("injection_known: " + problem)
        return 2
    current = signatures(row.get("findings"))
    if args.accept:
        try:
            save_baseline(current)
        except OSError as e:
            print("injection_known: could not write the baseline (%s)" % type(e).__name__)
            return 2
        print("baseline saved: %d file(s), %d finding(s) accepted as reviewed" % (len(current), sum(len(v) for v in current.values())))
        return 0
    baseline, problem = load_baseline()
    if problem:
        print("injection_known: " + problem)
        return 2
    new = new_findings(current, baseline)
    if not new:
        print("no new injection-scan findings (%d file(s) flagged, all reviewed)" % len(current) if current else "no injection-scan findings")
        return 0
    print("NEW injection-scan findings: " + json.dumps(new)[:MAX_PRINT])
    return 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
