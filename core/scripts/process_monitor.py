# -*- coding: utf-8 -*-
"""Process monitor: CPU and memory of the long-running programs the assistant depends on, with a flag for one that has grown too big (a leak).

Which programs? You list them, one per line, in logs/process_monitor_watch.txt. A line is a piece of the program's command line (a script name is the
usual choice) and, after a bar, an optional memory limit in MB for that program:

    # one watched program per line:  command-line fragment [| limit in MB]
    my_voice_server.py
    my_worker.py | 800

With no file (and no --marker) there is nothing to watch and the script says so. The default limit is 1500 MB (--limit-mb).
    python process_monitor.py                     # print one line per matching process; exit 1 when one is over its limit
    python process_monitor.py --job               # for the scheduler: always exit 0; honours logs/process_monitor_off.txt
    python process_monitor.py --marker foo.py     # watch one more fragment this run (repeatable)
    python process_monitor.py --no-alert          # print only

A process over its limit writes one alert line and raises a tier-2 work item (kind process_high_mem), at most once per 6 hours per program. This script
NEVER stops or restarts anything: what to do about a leak is for the owner to decide.
Needs the optional package psutil (pip install psutil); without it the script prints one line and exits 0.
Pure core (unit tested): parse_watch, match_marker, check_processes."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = "process_monitor"
OFF_NAME = "process_monitor_off.txt"
WATCH_NAME = "process_monitor_watch.txt"
STATE_NAME = "process_monitor_state.json"
RAM_WARN_MB = 1500
REALERT_SEC = 6 * 3600
MIN_MARKER = 3
MAX_MARKER = 120
MAX_WATCHED = 40
MAX_MATCHES = 60


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or (_root() / "logs")).resolve()


def parse_watch(text, default_limit=RAM_WARN_MB):
    """Pure. The watch list text -> [(marker, limit_mb)]. Blank lines and # comments are skipped; a marker under 3 characters is ignored (it would match nearly everything);
    a limit that is not a positive number falls back to the default."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        marker, _, limit = line.partition("|")
        marker = marker.strip()[:MAX_MARKER]
        if len(marker) < MIN_MARKER:
            continue
        try:
            lim = float(limit.strip()) if limit.strip() else float(default_limit)
            if lim <= 0:
                lim = float(default_limit)
        except ValueError:
            lim = float(default_limit)
        out.append((marker, lim))
        if len(out) >= MAX_WATCHED:
            break
    return out


def load_watch(logs=None, default_limit=RAM_WARN_MB):
    p = (Path(logs) if logs else _logs()) / WATCH_NAME
    try:
        return parse_watch(p.read_text(encoding="utf-8", errors="replace"), default_limit)
    except OSError:
        return []


def match_marker(cmdline, watch):
    """Pure. The first (marker, limit) of `watch` that appears in the command line, or None."""
    for marker, limit in watch:
        if marker in cmdline:
            return marker, limit
    return None


def check_processes(watch, psutil_mod, own_pid=None, cpu_interval=0.1):
    """-> [{"marker", "pid", "cpu_percent", "mem_mb", "limit_mb", "high_mem"}] for every process whose command line contains a watched fragment.
    A process that vanishes or cannot be read is skipped; this script's own process is never counted; at most 60 matches are measured."""
    own_pid = os.getpid() if own_pid is None else own_pid
    results = []
    gone = (getattr(psutil_mod, "NoSuchProcess", ()), getattr(psutil_mod, "AccessDenied", ()), getattr(psutil_mod, "ZombieProcess", ()))
    skip = tuple(g for g in gone if isinstance(g, type)) + (OSError,)
    for p in psutil_mod.process_iter(["pid", "name", "cmdline", "memory_info"]):
        try:
            info = p.info
            if info.get("pid") == own_pid:
                continue
            cmdline = " ".join(str(x) for x in (info.get("cmdline") or []))
            hit = match_marker(cmdline, watch)
            if not hit:
                continue
            mem = info.get("memory_info")
            mem_mb = mem.rss / (1024 ** 2) if mem else 0
            results.append({"marker": hit[0], "pid": info["pid"], "cpu_percent": p.cpu_percent(interval=cpu_interval), "mem_mb": round(mem_mb, 1),
                            "limit_mb": hit[1], "high_mem": mem_mb > hit[1]})
        except skip:
            continue
        if len(results) >= MAX_MATCHES:
            break
    return results


def _load_state(logs):
    try:
        data = json.loads((Path(logs) / STATE_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(logs, state):
    p = Path(logs) / STATE_NAME
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(str(tmp), str(p))
    except OSError:
        pass


def _alert(text, marker):
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import notify
        notify.alert(text, source=SOURCE, intent_kind="process_high_mem", tier=2, value=3, dedupe="process-high-mem-" + marker[:60], logs=_logs())
        return True
    except Exception:
        return False


def alert_high(results, now=None):
    """Alert for each over-limit program not alerted about in the last 6 hours. -> the markers alerted."""
    now = time.time() if now is None else now
    logs = _logs()
    state = _load_state(logs)
    marks = state.get("alerted") if isinstance(state.get("alerted"), dict) else {}
    done = []
    for r in results:
        if not r["high_mem"] or r["marker"] in done:
            continue
        try:
            fresh = now - float(marks.get(r["marker"], 0)) >= REALERT_SEC
        except (TypeError, ValueError):
            fresh = True
        if fresh and _alert("%s (pid %s) uses %s MB, over its %g MB limit: it may be leaking. Decide whether to restart it; nothing was stopped." % (
                r["marker"], r["pid"], r["mem_mb"], r["limit_mb"]), r["marker"]):
            marks[r["marker"]] = now
            done.append(r["marker"])
    if done:
        state["alerted"] = marks
        _save_state(logs, state)
    return done


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="CPU and memory of the watched programs")
    ap.add_argument("--marker", action="append", default=[])
    ap.add_argument("--limit-mb", type=float, default=RAM_WARN_MB)
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--job", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if a.limit_mb <= 0:
        print("bad value: --limit-mb must be above 0")
        return 2
    if a.job and (_logs() / OFF_NAME).exists():
        print("process_monitor: off")
        return 0
    watch = load_watch(default_limit=a.limit_mb) + parse_watch("\n".join(a.marker), a.limit_mb)
    if not watch:
        print("process_monitor: nothing to watch. List command-line fragments, one per line, in %s" % (_logs() / WATCH_NAME))
        return 0
    try:
        import psutil
    except ImportError:
        print("process_monitor: the optional package psutil is not installed (pip install psutil)")
        return 0
    results = check_processes(watch, psutil)
    for r in results:
        print("[%s] %s (pid %s): %s%% CPU, %s MB (limit %g)" % ("HIGH MEM" if r["high_mem"] else "ok", r["marker"], r["pid"], r["cpu_percent"], r["mem_mb"], r["limit_mb"]))
    if not results:
        print("process_monitor: none of the %d watched program(s) is running" % len(watch))
    high = any(r["high_mem"] for r in results)
    if high and not a.no_alert:
        alert_high(results)
    return 0 if (a.job or not high) else 1


if __name__ == "__main__":
    sys.exit(main())
