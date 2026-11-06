# -*- coding: utf-8 -*-
"""Job log: a small JSON-lines run history for scheduled scripts, so "did it work, and how often?" is a query, not a grep.

One file per job, logs/runs/<job>.jsonl, one JSON object per line: {"timestamp", "job", "success", "detail", ...your extra fields}.

    import job_log
    job_log.log_run("nightly_report", success=True, detail="12 notes checked")
    job_log.log_run("nightly_report", success=False, detail="vault folder missing", seconds=0.4)
    job_log.summarize("nightly_report")      # 'nightly_report: 1/2 succeeded (last 2 runs).'

Command line (the detail text is read from standard input, so no quoting problems with odd characters):
    echo "12 notes checked" | python job_log.py log nightly_report true
    python job_log.py show nightly_report        # summary plus the last 10 runs
    python job_log.py list                       # every job that has a history, with its pass count

A job name may use letters, digits, dot, dash and underscore (anything else becomes an underscore) so a name can never point outside logs/runs/.
A history file that grows past 2 MB is cut to its newest 500 runs. Nothing here raises into the caller's script except log_run on a name that is empty.
Paths: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): safe_name, make_entry, parse_runs, summarize_runs."""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUBDIR = "runs"
MAX_BYTES = 2 * 1024 * 1024
KEEP_RUNS = 500
READ_CAP = 8 * 1024 * 1024
MAX_DETAIL = 20000


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or (_root() / "logs")).resolve()


def runs_dir():
    return _logs() / SUBDIR


def safe_name(job_name):
    """A job name that is safe as a file name: letters, digits, dot, dash, underscore; no leading dots; at most 80 characters. '' when nothing is left."""
    n = re.sub(r"[^A-Za-z0-9._-]", "_", str(job_name or "").strip()).lstrip(".")
    return n[:80]


def make_entry(job_name, success, detail="", extra=None, now=None):
    """Pure. The record for one run. The core fields always win over same-named extra fields."""
    now = now or dt.datetime.now()
    entry = dict(extra or {})
    entry.update({"timestamp": now.isoformat(timespec="seconds"), "job": str(job_name), "success": bool(success), "detail": str(detail or "")[:MAX_DETAIL]})
    return entry


def _path(job_name):
    n = safe_name(job_name)
    if not n:
        raise ValueError("job name is empty")
    return runs_dir() / (n + ".jsonl")


def _trim(p):
    """Keep the newest KEEP_RUNS lines of a history that grew past MAX_BYTES (written to a temp file and swapped in)."""
    try:
        if p.stat().st_size <= MAX_BYTES:
            return
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-KEEP_RUNS:]
        tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(p))
    except OSError:
        pass


def log_run(job_name, success, detail="", **extra):
    """Append one run to the job's history and return the entry. Raises ValueError for an empty job name; other write problems raise OSError to the caller."""
    p = _path(job_name)
    entry = make_entry(job_name, success, detail, extra)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    _trim(p)
    return entry


def parse_runs(text):
    """Pure. JSON-lines text -> the runs that parse and look like runs; broken lines are skipped."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "success" in row:
            out.append(row)
    return out


def read_runs(job_name, last_n=None):
    """The logged runs of a job, oldest first; the last `last_n` of them when given (0 or less gives none). A missing history is []."""
    try:
        p = _path(job_name)
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > READ_CAP:
                f.seek(size - READ_CAP)
            text = f.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    runs = parse_runs(text)
    if last_n is None:
        return runs
    return runs[-last_n:] if last_n > 0 else []


def summarize_runs(job_name, runs):
    if not runs:
        return "No runs logged for %r yet." % job_name
    passed = sum(1 for r in runs if r.get("success"))
    return "%s: %d/%d succeeded (last %d runs)." % (job_name, passed, len(runs), len(runs))


def summarize(job_name, last_n=30):
    """One line: how many of the last `last_n` runs succeeded."""
    return summarize_runs(job_name, read_runs(job_name, last_n=last_n))


def list_jobs():
    try:
        return sorted(p.stem for p in runs_dir().glob("*.jsonl"))
    except OSError:
        return []


def _parse_bool(word):
    w = str(word).strip().lower()
    if w in ("true", "1", "yes", "ok", "pass", "passed"):
        return True
    if w in ("false", "0", "no", "fail", "failed"):
        return False
    return None


def _read_stdin():
    try:
        buf = getattr(sys.stdin, "buffer", None)
        raw = buf.read(MAX_DETAIL * 4).decode("utf-8", errors="replace") if buf is not None else sys.stdin.read(MAX_DETAIL)
        return raw.strip()
    except Exception:
        return ""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    usage = "usage: job_log.py log <job> <true|false> (detail on stdin) | show <job> | list"
    if not argv or argv[0] in ("-h", "--help"):
        print(usage)
        return 0 if argv else 2
    cmd = argv[0]
    if cmd == "log" and len(argv) == 3:
        ok = _parse_bool(argv[2])
        if ok is None or not safe_name(argv[1]):
            print(usage)
            return 2
        try:
            log_run(argv[1], ok, _read_stdin())
        except (OSError, ValueError) as e:
            print("could not log: %s" % type(e).__name__)
            return 1
        return 0
    if cmd == "show" and len(argv) == 2:
        print(summarize(argv[1]))
        for r in read_runs(argv[1], last_n=10):
            print("  %s  %s  %s" % (r.get("timestamp", "?"), "OK" if r.get("success") else "FAIL", str(r.get("detail", ""))[:100].replace("\n", " ")))
        return 0
    if cmd == "list" and len(argv) == 1:
        for j in list_jobs():
            print(summarize(j))
        return 0
    print(usage)
    return 2


if __name__ == "__main__":
    sys.exit(main())
