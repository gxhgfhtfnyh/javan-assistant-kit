# -*- coding: utf-8 -*-
"""Daily scoreboard: a few self-measured numbers appended as one row a day to logs/scoreboard.csv, to show whether the assistant is getting better or worse.

    python scoreboard.py              # measure now, append one row, print it
    python scoreboard.py --dry-run    # measure and print, append nothing
    python scoreboard.py --show       # print the last 7 rows
    python scoreboard.py --tests      # also run the test suite of the scripts folder (slow; see below) for tests_passed / tests_failed
Columns (a column that cannot be measured is left blank, never 0, so a gap is not mistaken for a good score):
    boot_bytes            size of the boot file CLAUDE.md in the assistant folder. Lower is better: a smaller boot file is read in full and followed.
    scripts_total         scripts in the scripts folder.        scripts_untested   scripts with no test_<name>.py. Lower is better, 0 is the goal.
    tests_passed / tests_failed   only with --tests: the suite is run in a child process with HARNESS_* pointed at an empty temporary folder, so it cannot touch your logs.
    open_intents          work items still open, doing or waiting. Lower is better.
    intents_raised_24h / intents_closed_24h    what the work loop did in the last 24 hours (harness_report.summarize).
    autonomy_pct          of the closed work items that were judged, the share that drew no correction or nudge from the user (needs learn_outcomes.py). Higher is better.
    event_started_pct     share of the closed work items that started from a sensor event instead of from a message. Higher means more happens by itself.
    alerts_24h            lines in logs/alerts.txt in the last 24 hours. Lower is better.
    lesson_candidates     bullet lines in the vault note 05 - Resources/Lessons Candidates.md (written by learn.py): lessons waiting to be turned into rules.
    prompts_24h, user_corrections_24h, user_nudges_24h   from logs/user_prompts.jsonl (written by prompt_log_hook.py): how many prompts, how many read as a correction
                          ("that is wrong", "you forgot"), how many read as a nudge ("keep going"). The last two should fall towards 0. Blank until the hook has logged anything.
Kill switch: logs/scoreboard_off.txt.
Pure core (unit tested): count_reactions(prompts, now), count_alerts(lines, now), script_counts(names), parse_test_summary(text), migrate_header(path), append_row(path, row).
"""
import csv
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).parent.resolve()
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

ROOT = Path(os.environ.get("HARNESS_ROOT") or SCRIPTS.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

CSV_NAME = "scoreboard.csv"
OFF_NAME = "scoreboard_off.txt"
DAY = 86400
READ_CAP = 2_000_000
TEST_TIMEOUT = 900
COLS = ["date", "time", "boot_bytes", "scripts_total", "scripts_untested", "tests_passed", "tests_failed", "open_intents", "intents_raised_24h", "intents_closed_24h",
        "autonomy_pct", "event_started_pct", "alerts_24h", "lesson_candidates", "prompts_24h", "user_corrections_24h", "user_nudges_24h"]

# What reads as a correction or a nudge in a prompt (the same families learn_outcomes.py uses; "again" alone is not a correction).
CORRECTION_RX = (r"mistake|wrong|forgot|assum|dropped|losing? focus|even after|\bnot what i (asked|meant|wanted)|\bi (didn.?t|did not) (ask|want)"
                 r"|\bstop (doing|sending)\b|\bwhy (did|are) (u|you)\b|\bundo (that|this|it)\b")
NUDGE_RX = (r"^\W*(continue|keep going|go on|carry on)\W*$|you keep stopping|u keep stopping|why (have|did|are) (u|you) (stop|stopp)|did (u|you) forget"
            r"|are (u|you) (still there|alive)|still (waiting|nothing)")


def get_root():
    return Path(os.environ.get("HARNESS_ROOT") or SCRIPTS.parent).resolve()


def get_logs():
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()


def get_vault():
    return Path(os.environ.get("HARNESS_VAULT") or get_root() / "vault").resolve()


def get_scripts_dir():
    cand = get_root() / "scripts"
    return cand if cand.is_dir() else SCRIPTS


def csv_path():
    return get_logs() / CSV_NAME


# ---------- pure counters ----------
def count_reactions(prompts, now):
    """Pure. -> (corrections, nudges) among the prompts of the last 24 hours. prompts: [{ts, text}]."""
    corr, nudge = re.compile(CORRECTION_RX, re.I), re.compile(NUDGE_RX, re.I)
    c = n = 0
    for p in prompts or []:
        if not isinstance(p, dict) or not isinstance(p.get("ts"), (int, float)) or now - p["ts"] > DAY:
            continue
        text = str(p.get("text", ""))
        if nudge.search(text):
            n += 1
        elif corr.search(text):
            c += 1
    return c, n


def count_alerts(lines, now):
    """Pure. Lines of alerts.txt (starting '[YYYY-MM-DD HH:MM:SS]' or 'YYYY-MM-DD HH:MM:SS') from the last 24 hours."""
    n = 0
    for ln in lines or []:
        m = re.match(r"\[?(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", ln)
        if not m:
            continue
        try:
            if now - time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) <= DAY:
                n += 1
        except (ValueError, OverflowError):
            continue
    return n


def script_counts(names):
    """Pure. (scripts, untested scripts) among the file names of a scripts folder; tests, private helpers and conftest do not count as scripts."""
    have = set(names)
    scripts = [n for n in names if n.endswith(".py") and not n.startswith(("test_", "_")) and n != "conftest.py"]
    return len(scripts), sum(1 for n in scripts if ("test_" + n) not in have)


def parse_test_summary(text):
    """Pure. (passed, failed) from the last pytest summary line such as '3 failed, 120 passed, 1 warning in 4.2s'; failed includes errors. ('', '') when there is no summary."""
    for ln in reversed((text or "").strip().splitlines()):
        passed = re.search(r"(\d+) passed", ln)
        failed = re.search(r"(\d+) failed", ln)
        errors = re.search(r"(\d+) errors?\b", ln)
        if (passed or failed or errors) and re.search(r"\bin [\d.]+s", ln):
            return int(passed.group(1)) if passed else 0, (int(failed.group(1)) if failed else 0) + (int(errors.group(1)) if errors else 0)
    return "", ""


# ---------- impure measurements ----------
def _tail_lines(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - READ_CAP))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    return lines[1:] if size > READ_CAP else lines


def boot_bytes():
    try:
        return (get_root() / "CLAUDE.md").stat().st_size
    except OSError:
        return ""


def folder_script_counts():
    try:
        return script_counts([p.name for p in get_scripts_dir().iterdir() if p.is_file()])
    except OSError:
        return "", ""


def lesson_candidates():
    lines = _tail_lines(get_vault() / "05 - Resources" / "Lessons Candidates.md")
    return "" if lines is None else sum(1 for ln in lines if re.match(r"\s*[-*]\s+\S", ln))


def run_tests(scripts_dir=None, runner=None, timeout=TEST_TIMEOUT):
    """Run pytest over the scripts folder in a child process whose HARNESS_* variables point at an empty temporary folder. -> (passed, failed), or ('', '') when it cannot be run."""
    runner = runner or subprocess.run
    d = Path(scripts_dir) if scripts_dir else get_scripts_dir()
    try:
        with tempfile.TemporaryDirectory(prefix="scoreboard_") as tmp:
            env = dict(os.environ, HARNESS_ROOT=tmp, HARNESS_LOGS=os.path.join(tmp, "logs"), HARNESS_VAULT=os.path.join(tmp, "vault"), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
            r = runner([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", str(d)], cwd=str(d), env=env, capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
        return parse_test_summary((r.stdout or "") + "\n" + (r.stderr or ""))
    except Exception:
        return "", ""


def intent_numbers(now, logs):
    """-> dict of the work-loop columns, all blank when the harness_report module or the logs are unavailable."""
    blank = {"open_intents": "", "intents_raised_24h": "", "intents_closed_24h": "", "autonomy_pct": "", "event_started_pct": ""}
    try:
        import harness_report
        s = harness_report.collect(now=now, logs=logs)
    except Exception:
        return blank

    def v(x):
        return "" if x is None else x
    return {"open_intents": s["open_now"], "intents_raised_24h": s["raised"], "intents_closed_24h": s["closed"],
            "autonomy_pct": v(s["accepted_pct"]), "event_started_pct": v(s["event_started_pct"])}


def measure(now=None, with_tests=False):
    now = time.time() if now is None else now
    logs = get_logs()
    total, untested = folder_script_counts()
    passed, failed = run_tests() if with_tests else ("", "")
    alerts = _tail_lines(logs / "alerts.txt")
    try:
        import prompt_log_hook
        prompts = prompt_log_hook.read_prompts(logs, since=now - DAY)
        has_feed = (logs / prompt_log_hook.LOG_NAME).exists()
    except Exception:
        prompts, has_feed = [], False
    corr, nudge = count_reactions(prompts, now) if has_feed else ("", "")
    row = {"date": time.strftime("%Y-%m-%d", time.localtime(now)), "time": time.strftime("%H:%M", time.localtime(now)), "boot_bytes": boot_bytes(),
           "scripts_total": total, "scripts_untested": untested, "tests_passed": passed, "tests_failed": failed,
           "alerts_24h": "" if alerts is None else count_alerts(alerts, now), "lesson_candidates": lesson_candidates(),
           "prompts_24h": len(prompts) if has_feed else "", "user_corrections_24h": corr, "user_nudges_24h": nudge}
    row.update(intent_numbers(now, logs))
    return {k: row.get(k, "") for k in COLS}


# ---------- the csv ----------
def migrate_header(path):
    """If the CSV on disk has a header other than COLS, rewrite it with the current COLS (old columns kept by name, new ones blank) so appended rows stay aligned."""
    path = Path(path)
    if not path.exists():
        return False
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rd = csv.reader(f)
            header = next(rd, None)
            if header is None or header == COLS:
                return False
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, COLS, restval="", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, path)
        return True
    except (OSError, csv.Error):
        return False


def append_row(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    migrate_header(path)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, COLS, restval="", extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def read_rows(path):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error):
        return []


def fmt(row):
    return ", ".join("%s=%s" % (k, row.get(k, "")) for k in COLS)


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if "--show" in argv:
        for r in read_rows(csv_path())[-7:]:
            print(fmt(r))
        return 0
    if (get_logs() / OFF_NAME).exists():
        print("scoreboard: off")
        return 0
    row = measure(with_tests="--tests" in argv)
    if "--dry-run" not in argv:
        try:
            append_row(csv_path(), row)
        except OSError as e:
            print("scoreboard: could not write %s (%s)" % (csv_path(), type(e).__name__))
    print(fmt(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
