# -*- coding: utf-8 -*-
"""Lab report: one mechanical summary of the assistant's own logs and checks, so a morning report does not depend on anyone's memory.

Reads logs only. Writes ONE note, vault/05 - Resources/Lab Report.md (overwritten each run, so it never piles up), and prints the same text.

    python lab_report.py              # build, print, write the note, make sure VAULT-INDEX.md lists it
    python lab_report.py --no-write   # print only
    python lab_report.py --job        # for the scheduler: same as the first line, always exit 0, honours logs/lab_report_off.txt
    python lab_report.py --no-index   # write the note but do not touch VAULT-INDEX.md
    python lab_report.py --self-test  # also run scripts/self_test.py (if you have one) and show its tally

Sections (each one only reads a log; a log that does not exist yet says so in one line):
  Alerts          lines in logs/alerts.txt in the last 24 hours, by source
  Work list       logs/intents.jsonl: open items by tier, the oldest open one; logs/outcomes.jsonl: what was closed in the last 24 hours and how
  Claim gate      logs/claim_gate.jsonl, last 7 days: replies that claim success, with no tool call, in turns with errors, evidence gaps, refusals
  Recall eval     logs/memory_eval.jsonl (written by recall_eval.py): the latest score and the change from the one before
  Harness health  logs/harness_health.json (written by harness_health.py): invariants green out of measured, and the red ones
  Scheduled jobs  logs/job_runner_state.json against scripts/jobs.json: jobs failing or enabled but never run
  Hygiene         the checks of hygiene_audit.py (skipped quietly when that script is missing)
  Self-test       the last tally in logs/self_test.log, or a live run with --self-test

Paths: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Pure helpers (parse_alert_line, summarize_*, tally_from_text, ensure_indexed) take parsed data so they can be tested."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OFF_NAME = "lab_report_off.txt"
NOTE_NAME = "Lab Report.md"
NOTE_TITLE = "Lab Report"
READ_CAP = 4 * 1024 * 1024
DAY = 86400
CLAIM_DAYS = 7
SELF_TEST_TIMEOUT = 300


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or (_root() / "logs")).resolve()


def _vault():
    return Path(os.environ.get("HARNESS_VAULT") or (_root() / "vault")).resolve()


def _scripts():
    return HERE


# ---------- reading ----------
def read_text_tail(path, cap=READ_CAP):
    """The last `cap` bytes of a text file (the whole file when smaller), or '' when it cannot be read."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > cap:
                f.seek(size - cap)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_jsonl(name, logs=None):
    """The rows (dicts) of logs/<name>; lines that do not parse are skipped; a missing file is []."""
    rows = []
    for line in read_text_tail(Path(logs or _logs()) / name).splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def read_json(name, logs=None):
    try:
        data = json.loads(read_text_tail(Path(logs or _logs()) / name) or "null")
        return data if isinstance(data, (dict, list)) else None
    except ValueError:
        return None


def _epoch(ts):
    """'2026-10-31 13:00:00' or '2026-10-31T13:00:00Z' -> epoch seconds (local time for the first, UTC for the Z form), else None."""
    s = str(ts or "").strip()
    try:
        if s.endswith("Z"):
            import calendar
            return calendar.timegm(time.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"))
        return time.mktime(time.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


# ---------- pure summaries ----------
ALERT_RE = re.compile(r"^\[?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]?\s*(?:([A-Za-z][\w.\-]{0,40}):\s)?")


def parse_alert_line(line):
    """'[2026-10-31 13:00:00] source: text' (also without brackets or source) -> (epoch, source or 'other'); (None, None) for a line that is not an alert."""
    m = ALERT_RE.match(line or "")
    if not m:
        return None, None
    return _epoch(m.group(1)), (m.group(2) or "other")


def summarize_alerts(lines, now, window=DAY):
    """-> {"total": alert lines in the window, "by_source": {source: count}}."""
    by = {}
    total = 0
    for ln in lines:
        t, src = parse_alert_line(ln)
        if t is not None and 0 <= now - t <= window:
            total += 1
            by[src] = by.get(src, 0) + 1
    return {"total": total, "by_source": by}


def summarize_intents(intents, now):
    """-> {"open": n, "by_tier": {tier: n}, "oldest_days": float or None, "dropped": n} for the work list."""
    open_ = [i for i in intents if isinstance(i, dict) and i.get("status") in ("open", "doing", "waiting")]
    by = {}
    for i in open_:
        by[int(i.get("tier", 1))] = by.get(int(i.get("tier", 1)), 0) + 1
    ages = [now - float(i["epoch"]) for i in open_ if isinstance(i.get("epoch"), (int, float))]
    return {"open": len(open_), "by_tier": dict(sorted(by.items())), "oldest_days": round(max(ages) / DAY, 1) if ages else None,
            "dropped": sum(1 for i in intents if isinstance(i, dict) and i.get("status") == "dropped")}


def summarize_outcomes(rows, now, window=DAY):
    """-> {result: count} of the outcomes recorded in the window."""
    out = {}
    for r in rows:
        t = _epoch(r.get("ts"))
        if t is not None and 0 <= now - t <= window:
            k = str(r.get("result") or "?")
            out[k] = out.get(k, 0) + 1
    return out


def summarize_claims(recs, now=None, days=CLAIM_DAYS):
    """claim_gate records (last `days` days when `now` is given) -> counts of claim replies, flagged replies, evidence gaps, refusals and offers."""
    if now is not None:
        recs = [r for r in recs if (_epoch(r.get("ts")) or 0) >= now - days * DAY]
    claims = [r for r in recs if r.get("claims")]
    return {"claim_replies": len(claims), "flagged_no_tools": sum(1 for r in claims if r.get("flagged_no_tools")),
            "flagged_errors": sum(1 for r in claims if r.get("flagged_errors")), "gaps": sum(1 for r in recs if r.get("gaps")),
            "refusals": sum(1 for r in recs if r.get("refusals")), "offers": sum(int(r.get("offers") or 0) for r in recs)}


def summarize_recall(rows):
    """memory_eval rows -> (text for the latest score and its change, or None when there are no usable rows)."""
    good = [r for r in rows if isinstance(r.get("total"), int) and isinstance(r.get("passed"), int) and r["total"] > 0]
    if not good:
        return None
    last = good[-1]
    pct = 100.0 * last["passed"] / last["total"]
    text = "%d/%d (%.0f%%) at %s" % (last["passed"], last["total"], pct, str(last.get("timestamp", "?"))[:16])
    if len(good) > 1:
        prev = 100.0 * good[-2]["passed"] / good[-2]["total"]
        text += ", change from the run before: %+.0f points" % (pct - prev)
    return text


def summarize_health(state):
    """The harness_health.json content -> one line, or None."""
    if not isinstance(state, dict) or "green" not in state:
        return None
    red = ["%s (%s)" % (k, str((v[1] if isinstance(v, list) and len(v) > 1 else ""))[:70]) for k, v in sorted((state.get("results") or {}).items())
           if isinstance(v, list) and v and v[0] == "red"]
    return "%s of %s measured invariants green%s (checked %s)" % (state.get("green"), state.get("measured"), "; red: " + "; ".join(red) if red else "", str(state.get("ts", "?"))[:16])


def summarize_jobs(state, jobs):
    """job_runner state + job table -> {"jobs": n, "failing": [(id, fails, last_code)], "never_ran": [ids of enabled jobs]}."""
    state = state if isinstance(state, dict) else {}
    failing, never = [], []
    for j in jobs or []:
        if not isinstance(j, dict) or not j.get("id"):
            continue
        st = state.get(j["id"]) or {}
        if int(st.get("fails", 0) or 0) > 0:
            failing.append((j["id"], int(st["fails"]), st.get("last_code")))
        if j.get("enabled", True) is not False and not st.get("last_run"):
            never.append(j["id"])
    return {"jobs": len([j for j in jobs or [] if isinstance(j, dict) and j.get("id")]), "failing": failing, "never_ran": never}


def tally_from_text(text):
    """The last 'N/M passed' or pytest summary line ('12 passed, 1 failed') in a text, else None."""
    best = None
    for ln in (text or "").splitlines():
        if re.search(r"\d+/\d+ passed", ln) or re.search(r"\d+ passed", ln) or re.search(r"\d+ failed", ln):
            best = ln.strip()[:160]
    return best


# ---------- the report ----------
def _section_alerts(now):
    text = read_text_tail(_logs() / "alerts.txt")
    if not text:
        return ["- no alerts file yet (logs/alerts.txt)"]
    s = summarize_alerts(text.splitlines(), now)
    top = ", ".join("%s=%d" % kv for kv in sorted(s["by_source"].items(), key=lambda kv: (-kv[1], kv[0]))[:6])
    return ["- alert lines in the last 24 hours: %d%s" % (s["total"], " (" + top + ")" if top else "")]


def _section_work(now):
    intents = read_jsonl("intents.jsonl")
    out = []
    if not intents:
        out.append("- no work items yet (logs/intents.jsonl)")
    else:
        s = summarize_intents(intents, now)
        tiers = ", ".join("tier%d=%d" % kv for kv in s["by_tier"].items())
        out.append("- open: %d%s | oldest open: %s | dropped after failing twice: %d" % (
            s["open"], " (" + tiers + ")" if tiers else "", "%s days" % s["oldest_days"] if s["oldest_days"] is not None else "-", s["dropped"]))
    o = summarize_outcomes(read_jsonl("outcomes.jsonl"), now)
    out.append("- closed in the last 24 hours: %s" % (", ".join("%s=%d" % kv for kv in sorted(o.items())) or "none"))
    return out


def _section_claims(now):
    recs = read_jsonl("claim_gate.jsonl")
    if not recs:
        return ["- no claim gate log yet (logs/claim_gate.jsonl)"]
    c = summarize_claims(recs, now)
    return ["- replies with claim words: %(claim_replies)d | with zero tool calls: %(flagged_no_tools)d | in turns with tool errors: %(flagged_errors)d | "
            "evidence gaps: %(gaps)d | refusals: %(refusals)d | optional offers counted: %(offers)d" % c]


def _section_recall():
    t = summarize_recall(read_jsonl("memory_eval.jsonl"))
    return ["- recall eval, latest: %s" % t] if t else ["- no recall eval yet (python scripts/recall_eval.py writes logs/memory_eval.jsonl)"]


def _section_health():
    t = summarize_health(read_json("harness_health.json"))
    return ["- " + t] if t else ["- no harness health result yet (logs/harness_health.json)"]


def _section_jobs():
    jobs = read_json_file(_scripts() / "jobs.json")
    if not isinstance(jobs, list):
        return ["- no job table (scripts/jobs.json)"]
    s = summarize_jobs(read_json("job_runner_state.json"), jobs)
    bits = ["%d job(s) in the table" % s["jobs"]]
    bits.append("failing: " + (", ".join("%s (fails %d, last code %s)" % f for f in s["failing"]) or "none"))
    bits.append("enabled but never run: " + (", ".join(s["never_ran"][:8]) + (" ..." if len(s["never_ran"]) > 8 else "") if s["never_ran"] else "none"))
    return ["- " + " | ".join(bits)]


def read_json_file(path):
    try:
        return json.loads(read_text_tail(path) or "null")
    except ValueError:
        return None


def _section_hygiene():
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import hygiene_audit
        lines, flags = hygiene_audit.build()
        return lines + ["- FLAGS: " + ("; ".join(flags) if flags else "none")]
    except ImportError:
        return None
    except Exception as e:
        return ["- hygiene audit failed (%s)" % type(e).__name__]


def _section_self_test(run_now):
    script = _scripts() / "self_test.py"
    if run_now:
        if not script.exists():
            return ["- self-test: scripts/self_test.py does not exist, nothing to run"]
        try:
            r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=SELF_TEST_TIMEOUT, cwd=str(_scripts()),
                               encoding="utf-8", errors="replace")
            lines = (r.stdout or "").splitlines()
            fails = [l[:110] for l in lines if l.startswith("FAIL")]
            return ["- self-test (run now): %s%s" % (tally_from_text(r.stdout) or "no tally", "; FAILED: " + " | ".join(fails) if fails else "")]
        except Exception as e:
            return ["- self-test: could not run (%s)" % type(e).__name__]
    t = tally_from_text(read_text_tail(_logs() / "self_test.log"))
    return ["- self-test, last logged: %s" % t] if t else ["- no self-test result yet (logs/self_test.log; --self-test runs scripts/self_test.py if you have one)"]


def build(now=None, self_test=False, hygiene=True):
    now = time.time() if now is None else now
    out = ["# Lab report %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), "",
           "Mechanical summary of the assistant's own logs, written by `python scripts/lab_report.py` and overwritten each run.", "",
           "## Alerts"] + _section_alerts(now)
    out += ["", "## Work list"] + _section_work(now)
    out += ["", "## Claim gate (last %d days)" % CLAIM_DAYS] + _section_claims(now)
    out += ["", "## Recall eval"] + _section_recall()
    out += ["", "## Harness health"] + _section_health()
    out += ["", "## Scheduled jobs"] + _section_jobs()
    if hygiene:
        h = _section_hygiene()
        if h:
            out += ["", "## Hygiene (automatic)"] + h
    out += ["", "## Self-test"] + _section_self_test(self_test)
    return "\n".join(out)


# ---------- writing ----------
def _atomic_write(path, text):
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def write_note(text, vault=None, now=None):
    """Write the report note under the vault's 05 - Resources folder. -> the path, or None when the vault folder does not exist."""
    v = Path(vault) if vault else _vault()
    if not v.is_dir():
        return None
    folder = v / "05 - Resources"
    folder.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))
    p = folder / NOTE_NAME
    _atomic_write(p, "---\nstatus: active\ntype: resource\ntopic: harness\nchecked: %s\n---\n%s\n" % (day, text))
    return p


INDEX_LINE = "- [[%s]]: the daily mechanical summary of the assistant's own logs (written by `python scripts/lab_report.py`, overwritten each run)" % NOTE_TITLE


def ensure_indexed(vault=None):
    """Make sure VAULT-INDEX.md lists the report note (an unlisted note is one a future session will not find). Idempotent. -> True when a line was added."""
    p = (Path(vault) if vault else _vault()) / "VAULT-INDEX.md"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False
    if re.search(r"\[\[%s(\||\]\])" % re.escape(NOTE_TITLE), text):
        return False
    m = re.search(r"^## Generated notes[ \t]*\r?\n", text, re.M)
    if m:
        new = text[:m.end()] + INDEX_LINE + "\n" + text[m.end():]
    else:
        block = "## Generated notes\n" + INDEX_LINE + "\n\n"
        r = re.search(r"^## Rules", text, re.M)
        new = text[:r.start()] + block + text[r.start():] if r else text.rstrip("\n") + "\n\n" + block.rstrip("\n") + "\n"
    try:
        _atomic_write(p, new)
    except OSError:
        return False
    return True


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="daily mechanical summary of the assistant's logs")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--no-index", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--job", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if a.job and (_logs() / OFF_NAME).exists():
        print("lab_report: off")
        return 0
    text = build(self_test=a.self_test)
    print(text)
    if not a.no_write:
        try:
            p = write_note(text)
            if p is None:
                print("lab_report: the vault folder %s does not exist, so no note was written" % _vault())
            elif not a.no_index:
                ensure_indexed()
        except OSError as e:
            print("lab_report: could not write the note (%s)" % type(e).__name__)
            return 0 if a.job else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
