# -*- coding: utf-8 -*-
"""Network baseline check: a cheap tripwire for beaconing and data exfiltration.

For every running program that belongs to the assistant (its exe path or command line contains the assistant's install folder, or a fragment you list in
logs/network_baseline_markers.txt, one per line, '#' starts a comment) it records the remote addresses the program is talking to. The first 24 hours only learn.
After that, a (program, remote address) pair never seen before raises one alert: "somewhere this program has never talked to".
Loopback addresses are ignored. The baseline only ever grows, so normal churn (a new CDN address) alerts once and is then quiet.

    python scripts/network_baseline_check.py [--dry-run] [--learn-hours N]

Needs the optional package psutil (pip install psutil); without it the script says so and exits 0. On macOS other users' connections need root, so some programs may be
skipped (the count is reported). Files in logs/: network_baseline.json (the baseline), network_baseline_check.jsonl (one line per run), network_baseline_markers.txt (optional).
Kill switch: create logs/network_baseline_check_off.txt. --dry-run changes nothing (no baseline, log or alert). A timed job always exits 0; findings go through notify.alert.
Pure core (unit tested): is_local_ip, matches, snapshot, load_baseline, evaluate, check."""
import argparse
import hashlib
import ipaddress
import json
import os
import sys
import time
from pathlib import Path

NAME = "network_baseline_check"
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

BASELINE_NAME = "network_baseline.json"
LOG_NAME = "network_baseline_check.jsonl"
MARKERS_NAME = "network_baseline_markers.txt"
OFF_NAME = "network_baseline_check_off.txt"
LEARN_HOURS = 24
MAX_PAIRS = 5000
MAX_READ = 5_000_000
MAX_LOG_BYTES = 1_000_000


def logs_dir():
    return Path(LOGS)


def root_dir():
    return Path(ROOT)


def _psutil():
    try:
        import psutil
        return psutil
    except Exception:
        return None


def _alert(text, **kw):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        return notify.alert(text, source=NAME, logs=str(logs_dir()), **kw)
    except Exception:
        return None


def _norm(text):
    return str(text or "").replace("\\", "/").lower()


def is_local_ip(ip):
    """True for loopback addresses (127.x, ::1, ::ffff:127.x). Anything unparseable counts as NOT local so it is never hidden."""
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None) is not None:
        a = a.ipv4_mapped
    return a.is_loopback


def read_markers(logs=None, root=None):
    """The path fragments that mark a program as the assistant's: its install folder plus the lines of logs/network_baseline_markers.txt."""
    out = [_norm(root or root_dir())]
    try:
        text = (Path(logs or logs_dir()) / MARKERS_NAME).read_text(encoding="utf-8", errors="replace")[:MAX_READ]
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(_norm(line))
    return [m for m in out if m]


def matches(exe, cmdline, markers):
    hay = _norm(exe) + " " + _norm(" ".join(cmdline or []))
    return any(m in hay for m in markers)


def snapshot(ps, markers):
    """-> (sorted 'program|remote ip' pairs, number of programs whose connections could not be read)."""
    pairs, denied = set(), 0
    for p in ps.process_iter(["pid", "exe", "cmdline"]):
        try:
            exe = p.info.get("exe") or ""
            if not matches(exe, p.info.get("cmdline"), markers):
                continue
            getter = getattr(p, "net_connections", None) or p.connections           # connections() is the old name, deprecated in psutil 6
            for conn in getter(kind="inet"):
                if conn.raddr and conn.raddr.ip and not is_local_ip(conn.raddr.ip):
                    pairs.add("%s|%s" % (Path(exe).name or "unknown", conn.raddr.ip))
        except ps.AccessDenied:
            denied += 1
        except Exception:                                                                 # the process ended meanwhile, or anything else odd: skip it
            continue
    return sorted(pairs), denied


def load_baseline(path):
    """-> (data, status). status is 'missing', 'ok' or 'corrupt'; data is {'created': epoch, 'pairs': [..]} or None. A bare list (the old format) is accepted."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_READ:
            return None, "corrupt"
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "corrupt"
    if isinstance(raw, list):
        raw = {"created": 0, "pairs": raw}
    if not isinstance(raw, dict) or not isinstance(raw.get("pairs"), list) or not all(isinstance(x, str) for x in raw["pairs"]):
        return None, "corrupt"
    created = raw.get("created", 0)
    return {"created": created if isinstance(created, (int, float)) and not isinstance(created, bool) else 0, "pairs": raw["pairs"]}, "ok"


def save_baseline(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def append_log(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".old"))
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def evaluate(current, baseline, now, learn_hours=LEARN_HOURS):
    """Pure. -> (new_pairs, learning, first_run, updated_baseline). During the learning window new pairs are absorbed without being reported."""
    if baseline is None:
        pairs = sorted(set(current))[:MAX_PAIRS]
        return [], True, True, {"created": now, "pairs": pairs}
    known = set(baseline["pairs"])
    new = sorted(set(current) - known)
    learning = (now - float(baseline.get("created") or 0)) < learn_hours * 3600 if baseline.get("created") else False
    merged = sorted(known | set(current))
    if len(merged) > MAX_PAIRS:
        merged = sorted(known)[:MAX_PAIRS]                                                # full: stop growing rather than forget what is trusted
    return ([] if learning else new), learning, False, {"created": baseline.get("created") or 0, "pairs": merged}


def check(now=None, dry_run=False, learn_hours=LEARN_HOURS, ps=None):
    now = time.time() if now is None else now
    logs = logs_dir()
    if (logs / OFF_NAME).exists():
        return {"off": True}
    ps = ps or _psutil()
    if ps is None:
        return {"skipped": "psutil is not installed (pip install psutil)"}
    current, denied = snapshot(ps, read_markers(logs))
    base_path = logs / BASELINE_NAME
    baseline, status = load_baseline(base_path)
    if status == "corrupt":
        if not dry_run:
            try:
                os.replace(base_path, base_path.with_name(base_path.name + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(now))))
            except OSError:
                pass
            _alert("the network baseline file could not be read; it was moved aside and learning restarts", intent_kind="network_baseline_reset", tier=2, value=3, dedupe="netbase-corrupt")
        baseline = None
    new, learning, first_run, updated = evaluate(current, baseline, now, learn_hours)
    result = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), "first_run": first_run, "learning": learning, "active_connections": len(current),
              "new_destinations": new, "unreadable_programs": denied, "dry_run": bool(dry_run)}
    if dry_run:
        return result
    save_baseline(base_path, updated)
    append_log(logs / LOG_NAME, result)
    if new:
        key = hashlib.sha1("|".join(new).encode("utf-8")).hexdigest()[:10]
        _alert("%d new network destination(s) for assistant programs, e.g. %s" % (len(new), "; ".join(new[:3])), intent_kind="network_new_destination", tier=2, value=3,
               dedupe="netbase-" + key)
    return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog=NAME, description="flag first-seen network destinations of the assistant's programs")
    ap.add_argument("--dry-run", action="store_true", help="change nothing: no baseline, log or alert")
    ap.add_argument("--learn-hours", type=float, default=LEARN_HOURS)
    a = ap.parse_args(argv)
    try:
        r = check(dry_run=a.dry_run, learn_hours=a.learn_hours)
    except Exception as e:                                                  # a timed job must never fail the runner
        print("[ERROR] %s: %s" % (type(e).__name__, str(e)[:120]))
        return 0
    if r.get("off"):
        print("[OFF] logs/%s exists" % OFF_NAME)
    elif r.get("skipped"):
        print("[SKIPPED] %s" % r["skipped"])
    elif r["first_run"]:
        print("[BASELINE_SET] %d active connection(s)" % r["active_connections"])
    elif r["new_destinations"]:
        print("[NEW] %s" % r["new_destinations"])
    elif r["learning"]:
        print("[LEARNING] %d active connection(s)" % r["active_connections"])
    else:
        print("[OK] no new destinations (%d active)" % r["active_connections"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
