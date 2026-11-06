# -*- coding: utf-8 -*-
"""Machine-checked goals: named numbers compared to targets, so "am I done?" is a measurement and not a feeling.

Why: a stop hook that only looks at the work list lets an assistant quietly stop when a test is red or the list merely looks empty. Goals close that gap: logs/goals.json
lists the numbers that must be true, each one read by a cheap named reader below, and unmet() says which are not. A stop hook (wired by you, not by this file) can ask
unmet() and hold the stop open while a goal is unmet. Nothing here acts: it reads logs and prints.

logs/goals.json is a list (or {"goals": [...]}) of goals. One goal:
    {"name": "no failed work items today", "metric": "failed_outcomes_24h", "op": "<=", "target": 0, "next": "read logs/outcomes.jsonl, fix the cause of each fail"}
        name    short text shown in the report and the stop reason (required)
        metric  one of the readers listed below (required)
        op      "<=" or ">=" (default ">=")
        target  a number (required)
        next    an optional hint for what to do when the goal is unmet
Starter file (what `python goals_check.py --init` writes when there is none; it never overwrites):
    [{"name": "no failed work items in the last 24 hours", "metric": "failed_outcomes_24h", "op": "<=", "target": 0, "next": "..."},
     {"name": "every script has a test file", "metric": "untested_scripts", "op": "<=", "target": 0, "next": "..."}]

Metrics (each returns a number, or None when it cannot be measured, and None always counts as UNMET, never as met):
    open_intents            work items still open, doing or waiting (shadow items excluded)
    actionable_intents      work items the assistant may act on right now (tier 0 or 1, open, not leased, not shadow)
    failed_outcomes_24h     outcomes closed with result "fail" in the last 24 hours (logs/outcomes.jsonl)
    outcome_ok_pct_7d       percent of outcomes in the last 7 days that closed ok or skipped (None when there are none)
    recall_pass_pct         percent score of the latest run of recall_eval.py (logs/memory_eval.jsonl; None until it has run)
    untested_scripts        scripts in the scripts folder with no test_<name>.py next to them
    health_red              invariants that are red in the latest harness_health.py run (logs/harness_health.json; None if older than 24 hours or missing)
    health_green_pct        percent of measured invariants that are green in that run

Kill switch: logs/goals_check_off.txt makes unmet() return [] (goals stop holding anything); the command line report still prints.
    python goals_check.py             # every goal with its value and met / UNMET
    python goals_check.py --check     # same, exit 1 when any goal is unmet
    python goals_check.py --details   # also lists the scripts that have no test
    python goals_check.py --init      # write the starter logs/goals.json if there is none
Pure core (unit tested): is_met(value, op, target), evaluate(goals, readers), load_goals(path), unmet(goals, readers), block_reason(rows).
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

GOALS_NAME = "goals.json"
OFF_NAME = "goals_check_off.txt"
DAY = 86400
READ_CAP = 5_000_000          # never read more than this from the end of a log

STARTER_GOALS = [
    {"name": "no failed work items in the last 24 hours", "metric": "failed_outcomes_24h", "op": "<=", "target": 0,
     "next": "read logs/outcomes.jsonl, find each result fail, fix its cause, then close the work item again with evidence"},
    {"name": "every script has a test file", "metric": "untested_scripts", "op": "<=", "target": 0,
     "next": "run python scripts/goals_check.py --details and write the missing test_<name>.py files"},
]


def get_root():
    return Path(os.environ.get("HARNESS_ROOT") or SCRIPTS.parent).resolve()


def get_logs():
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()


def get_scripts_dir():
    """The folder whose scripts are checked for tests: ROOT/scripts when it exists, otherwise the folder this file is in."""
    cand = get_root() / "scripts"
    return cand if cand.is_dir() else SCRIPTS


def goals_path():
    return get_logs() / GOALS_NAME


def is_off():
    try:
        return (get_logs() / OFF_NAME).exists()
    except OSError:
        return False


# ---------- pure core ----------
def is_met(value, op, target):
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value <= target if op == "<=" else value >= target


def evaluate(goals, readers):
    """[(goal, value, met)] for every goal; a metric that cannot be read counts as unmet with value None (never silently met)."""
    out = []
    for g in goals:
        try:
            v = readers[g["metric"]]()
        except Exception:
            v = None
        out.append((g, v, is_met(v, g.get("op", ">="), g["target"])))
    return out


def _valid_goal(g):
    return (isinstance(g, dict) and isinstance(g.get("name"), str) and g["name"].strip() and isinstance(g.get("metric"), str)
            and g.get("op", ">=") in ("<=", ">=") and isinstance(g.get("target"), (int, float)) and not isinstance(g.get("target"), bool))


def _read_goals_raw(path=None):
    try:
        with open(path or goals_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        data = data.get("goals")
    return data if isinstance(data, list) else None


def load_goals(path=None):
    """The valid goals in logs/goals.json. A missing or unparseable file is []; a row that is not a well-formed goal is left out (see problems())."""
    return [g for g in (_read_goals_raw(path) or []) if _valid_goal(g)]


def problems(path=None):
    """Human-readable problems with logs/goals.json: unreadable file, malformed rows, unknown metrics."""
    raw = _read_goals_raw(path)
    if raw is None:
        if not os.path.exists(path or goals_path()):
            return ["logs/goals.json does not exist yet"]
        return ["logs/goals.json cannot be read as a list of goals"]
    out = []
    for n, g in enumerate(raw, 1):
        if not _valid_goal(g):
            out.append("goal %d is malformed (needs name, metric, target number, op <= or >=)" % n)
        elif g["metric"] not in READERS:
            out.append("goal %d (%s) names an unknown metric %r" % (n, g["name"], g["metric"]))
    return out


def unmet(goals=None, readers=None):
    """[(name, value, target, op, next_hint)] of goals not yet met. [] while logs/goals_check_off.txt exists."""
    if is_off():
        return []
    rows = evaluate(load_goals() if goals is None else goals, READERS if readers is None else readers)
    return [(g["name"], v, g["target"], g.get("op", ">="), g.get("next", "")) for g, v, ok in rows if not ok]


def block_reason(rows, limit=3):
    """Pure. The text a stop hook can show when unmet() is not empty: which goals, their values, and what to do first. '' for no rows."""
    if not rows:
        return ""
    parts = []
    for name, value, target, op, hint in rows[:limit]:
        parts.append("%s: now %s, goal %s %s%s" % (name, "unmeasured" if value is None else value, op, target, (" (next: %s)" % hint) if hint else ""))
    more = " and %d more" % (len(rows) - limit) if len(rows) > limit else ""
    return "%d goal(s) not met yet: %s%s. Work on the first one before stopping, or say plainly what blocks it." % (len(rows), "; ".join(parts), more)


# ---------- readers (impure, read the logs of the folder named by HARNESS_*) ----------
def _intent_store():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import intent_store
    return intent_store


def _rows(name):
    store = _intent_store()
    return store.read_jsonl(name, str(get_logs()))


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(str(value)[:19], fmt))
        except (ValueError, TypeError):
            continue
    return None


def read_open_intents():
    return sum(1 for i in _rows("intents.jsonl") if isinstance(i, dict) and i.get("status") in ("open", "doing", "waiting") and not i.get("shadow"))


def read_actionable_intents():
    store = _intent_store()
    now = time.time()
    return sum(1 for i in _rows("intents.jsonl") if isinstance(i, dict) and store.actionable(i, now))


def _recent_outcomes(seconds, now=None):
    now = time.time() if now is None else now
    out = []
    for o in _rows("outcomes.jsonl"):
        t = _epoch(o.get("ts")) if isinstance(o, dict) else None
        if t is not None and now - t <= seconds:
            out.append(o)
    return out


def read_failed_outcomes_24h():
    return sum(1 for o in _recent_outcomes(DAY) if o.get("result") == "fail")


def read_outcome_ok_pct_7d():
    rows = _recent_outcomes(7 * DAY)
    if not rows:
        return None
    return round(100.0 * sum(1 for o in rows if o.get("result") in ("ok", "skipped")) / len(rows), 1)


def read_recall_pass_pct(path=None):
    """Percent score of the newest usable row of logs/memory_eval.jsonl (rows with a total of 0 are skipped)."""
    p = Path(path) if path else get_logs() / "memory_eval.jsonl"
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(max(0, size - READ_CAP))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    best = None
    for line in text.splitlines():
        try:
            r = json.loads(line)
            if isinstance(r, dict) and r.get("total") and r.get("passed") is not None:
                best = 100.0 * float(r["passed"]) / float(r["total"])
        except (ValueError, TypeError, ZeroDivisionError):
            continue
    return None if best is None else round(best, 1)


def untested_script_names(scripts_dir=None):
    """Scripts (not tests, not private helpers) in the folder that have no test_<name>.py next to them."""
    d = Path(scripts_dir) if scripts_dir else get_scripts_dir()
    try:
        names = sorted(p.name for p in d.iterdir() if p.is_file() and p.suffix == ".py")
    except OSError:
        return None
    have = set(names)
    return [n for n in names if not n.startswith(("test_", "_")) and n != "conftest.py" and ("test_" + n) not in have]


def read_untested_scripts():
    names = untested_script_names()
    return None if names is None else len(names)


def _health(max_age=DAY):
    try:
        with open(get_logs() / "harness_health.json", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    t = _epoch(data.get("ts"))
    if t is None or time.time() - t > max_age:
        return None
    return data


def read_health_red():
    data = _health()
    if data is None or not isinstance(data.get("results"), dict):
        return None
    return sum(1 for v in data["results"].values() if isinstance(v, (list, tuple)) and v and v[0] == "red")


def read_health_green_pct():
    data = _health()
    if data is None:
        return None
    measured, green = data.get("measured"), data.get("green")
    if not isinstance(measured, (int, float)) or not isinstance(green, (int, float)) or measured <= 0:
        return None
    return round(100.0 * green / measured, 1)


READERS = {
    "open_intents": read_open_intents,
    "actionable_intents": read_actionable_intents,
    "failed_outcomes_24h": read_failed_outcomes_24h,
    "outcome_ok_pct_7d": read_outcome_ok_pct_7d,
    "recall_pass_pct": read_recall_pass_pct,
    "untested_scripts": read_untested_scripts,
    "health_red": read_health_red,
    "health_green_pct": read_health_green_pct,
}


def write_starter(path=None):
    """Write the starter goals file. -> True when written, False when a goals file already exists (it is never overwritten)."""
    p = Path(path) if path else goals_path()
    if p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STARTER_GOALS, f, indent=1)
        f.write("\n")
    os.replace(tmp, p)
    return True


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if "--init" in argv:
        if write_starter():
            print("goals_check: wrote the starter goals to %s" % goals_path())
        else:
            print("goals_check: %s already exists, left as it is" % goals_path())
        return 0
    goals = load_goals()
    if not goals:
        print("goals_check: no goals yet. Run python scripts/goals_check.py --init to write a starter logs/goals.json.")
        return 0
    if is_off():
        print("goals_check: kill switch is on (logs/%s), goals hold nothing; values below are for information" % OFF_NAME)
    any_unmet = False
    for g, v, ok in evaluate(goals, READERS):
        any_unmet = any_unmet or not ok
        print("%-6s %s: %s (goal %s %s)%s" % ("met" if ok else "UNMET", g["name"], "unmeasured" if v is None else v, g.get("op", ">="), g["target"],
                                              "" if ok or not g.get("next") else "  next: " + g["next"]))
    for p in problems():
        print("note: " + p)
    if "--details" in argv:
        names = untested_script_names()
        print("scripts without a test: %s" % (", ".join(names) if names else ("none" if names is not None else "(scripts folder unreadable)")))
    return 1 if ("--check" in argv and any_unmet) else 0


if __name__ == "__main__":
    sys.exit(main())
