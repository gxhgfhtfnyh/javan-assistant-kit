# -*- coding: utf-8 -*-
"""Session log auto: a Stop-hook helper that writes what got done into today's daily note, so the log never depends on remembering.

Source: logs/outcomes.jsonl, the closed work items the intent store records ({ts, intent_id, action, evidence, result}). Each new row since the last run becomes one bullet
"Auto-log [HH:MM] result: what was done - evidence" in vault/01 - Daily Notes/YYYY-MM-DD.md.

Rules it keeps:
  * It writes into the vault ONLY through memory_write.py (the kit's one door: refuses secrets, holds instruction-like text in quarantine, keeps a log, appends and never
    rewrites). A closed item whose evidence looks like a secret is skipped, never logged; one that reads like an instruction is held in quarantine.
  * A cursor in logs/session_log_state.json ({"last_ts": "YYYY-MM-DD HH:MM:SS"}) remembers how far it got, so nothing is logged twice. With no cursor it starts at midnight today.
  * It waits until 5 new rows have piled up (so one tiny chore does not spam the note), unless the oldest waiting row is over 4 hours old or you pass --force.
  * At most 25 bullets per run; the rest become one summary bullet that points at outcomes.jsonl.
  * No daily notes folder in the vault = nothing happens (the cursor stays, nothing is lost). If today's note does not exist yet the first write creates it with bullets only;
    create it from the daily-note template at session start and the hook just appends to it.

    python session_log_auto.py            # as a hook: reads the hook JSON on stdin, prints nothing, never fails
    python session_log_auto.py --force    # log whatever is new now
    python session_log_auto.py --dry-run  # show what would be logged, change nothing
    python session_log_auto.py --init     # set the cursor to now without writing (start fresh)
Wire it as a Stop hook (timeout 10). Kill switch: a file logs/session_log_auto_off.txt.
Paths: ROOT is the parent of the scripts folder (HARNESS_ROOT overrides), the vault is ROOT/vault (HARNESS_VAULT), logs are ROOT/logs (HARNESS_LOGS).
Pure core (unit tested): norm_ts, collect_entries, format_entry, daily_note_rel, read_outcomes; run does the work."""
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

OUTCOMES_NAME = "outcomes.jsonl"
STATE_NAME = "session_log_state.json"
OFF_NAME = "session_log_auto_off.txt"
DAILY_FOLDER = "01 - Daily Notes"
MIN_ENTRIES = 5
MAX_WAIT_HOURS = 4
MAX_BULLETS = 25
MAX_READ = 2 * 1024 * 1024
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def get_root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def get_logs():
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()


def get_vault():
    return Path(os.environ.get("HARNESS_VAULT") or get_root() / "vault").resolve()


def norm_ts(value):
    """-> 'YYYY-MM-DD HH:MM:SS' for an ISO-looking string ('T' or a space between date and time, fractions and zones cut off), else None."""
    if not isinstance(value, str):
        return None
    s = value.strip().replace("T", " ")[:19]
    return s if TS_RE.match(s) else None


def read_outcomes(path, max_bytes=MAX_READ):
    """-> the rows of the last `max_bytes` of the outcomes file (a long history is not read in full). Missing file or bad lines give fewer rows, never an error."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - max_bytes))
            data = f.read()
            if size > max_bytes:
                data = data.split(b"\n", 1)[-1]                          # the first line is cut in the middle
    except OSError:
        return []
    rows = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def collect_entries(rows, since):
    """-> [{"ts", "id", "action", "evidence", "result"}] for rows strictly after `since`, oldest first. Rows without a readable time or an action are skipped."""
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ts = norm_ts(r.get("ts"))
        action = r.get("action")
        if not ts or ts <= since or not isinstance(action, str) or not action.strip():
            continue
        out.append({"ts": ts, "id": str(r.get("intent_id") or ""), "action": action, "evidence": str(r.get("evidence") or ""), "result": str(r.get("result") or "")})
    out.sort(key=lambda e: e["ts"])
    return out


def _one_line(text, limit):
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[:limit].rstrip() + "..."


def format_entry(e):
    """-> the text of one bullet (memory_write adds the leading dash and the date)."""
    head = "Auto-log [%s] %s: %s" % (e["ts"][11:16], e.get("result") or "done", _one_line(e["action"], 160))
    ev = _one_line(e.get("evidence", ""), 200)
    return head + (" - " + ev if ev else "")


def daily_note_rel(now):
    return "%s/%s.md" % (DAILY_FOLDER, now.strftime("%Y-%m-%d"))


def load_cursor(now):
    try:
        v = norm_ts(json.loads((get_logs() / STATE_NAME).read_text(encoding="utf-8"))["last_ts"])
        if v:
            return v
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return now.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")


def save_cursor(ts):
    target = get_logs() / STATE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps({"last_ts": ts}), encoding="utf-8")
    os.replace(tmp, target)


def overdue(entries, now):
    """True when the oldest waiting row is more than MAX_WAIT_HOURS old."""
    try:
        first = time.mktime(time.strptime(entries[0]["ts"], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, IndexError, OverflowError):
        return False
    return now.timestamp() - first > MAX_WAIT_HOURS * 3600


def remember(note, text):
    """Hand one line to memory_write.py, the only way a script here writes into the vault. -> its result dict; never raises."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import memory_write
        return memory_write.write_fact(note, text, source="script")
    except Exception as e:
        return {"status": "error", "reason": "%s: %s" % (type(e).__name__, str(e)[:120])}


def run(now=None, force=False, dry_run=False):
    """Log what is new. -> {"status": str, "written": int, "skipped": int, "pending": int, "lines": [text]}. Never raises."""
    res = {"status": "", "written": 0, "skipped": 0, "pending": 0, "lines": []}
    try:
        now = now or dt.datetime.now().replace(microsecond=0)
        if (get_logs() / OFF_NAME).exists():
            res["status"] = "switched off (logs/%s exists)" % OFF_NAME
            return res
        entries = collect_entries(read_outcomes(get_logs() / OUTCOMES_NAME), load_cursor(now))
        res["pending"] = len(entries)
        if not entries:
            res["status"] = "nothing new"
            return res
        if not force and len(entries) < MIN_ENTRIES and not overdue(entries, now):
            res["status"] = "waiting: %d of %d entries" % (len(entries), MIN_ENTRIES)
            return res
        if not (get_vault() / DAILY_FOLDER).is_dir():
            res["status"] = "no %s folder in the vault; nothing logged" % DAILY_FOLDER
            return res
        note = daily_note_rel(now)
        batch, rest = entries[:MAX_BULLETS], entries[MAX_BULLETS:]
        lines = [format_entry(e) for e in batch]
        if rest:
            lines.append("Auto-log: %d more closed items between %s and %s (see logs/%s)" % (len(rest), rest[0]["ts"][11:16], rest[-1]["ts"][11:16], OUTCOMES_NAME))
        res["lines"] = lines
        if dry_run:
            res["status"] = "dry run: %d bullet(s) would be added to %s" % (len(lines), note)
            return res
        done_ts = None
        for i, text in enumerate(lines):
            r = remember(note, text)
            status = r.get("status")
            if status == "error":                                           # a real write problem: stop here, the cursor stays before this entry so it is retried next time
                res["status"] = "stopped: %s" % r.get("reason", "write error")
                break
            if status == "wrote":
                res["written"] += 1
            else:                                                           # refused (looks like a secret) or quarantined (reads like an instruction): not logged, not retried
                res["skipped"] += 1
            done_ts = (batch[i] if i < len(batch) else entries[-1])["ts"]
        else:
            done_ts = entries[-1]["ts"]
            res["status"] = "appended %d entries to %s" % (res["written"], Path(note).name)
        if done_ts:
            save_cursor(done_ts)
    except Exception as e:                                                   # a hook must never break the turn
        res["status"] = "error: %s" % type(e).__name__
    return res


def drain_stdin():
    """Read and drop the hook JSON (bounded), so the sender never blocks. Nothing in it is needed."""
    try:
        if not sys.stdin.isatty():
            sys.stdin.buffer.read(1_000_000)
    except Exception:
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        force, dry, init = "--force" in argv, "--dry-run" in argv, "--init" in argv
        verbose = force or dry or init or "--verbose" in argv
        drain_stdin()
        if init:
            now = dt.datetime.now().replace(microsecond=0)
            save_cursor(now.strftime("%Y-%m-%d %H:%M:%S"))
            print("cursor set to", now.isoformat())
            return 0
        r = run(force=force, dry_run=dry)
        if verbose:
            print(r["status"])
            if dry:
                for line in r["lines"]:
                    print("  - " + line)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
