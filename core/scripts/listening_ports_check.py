# -*- coding: utf-8 -*-
"""Listening ports check: are the assistant's own local servers reachable from the network?

If you run small servers next to the assistant (a speech or avatar helper, a web page, a bridge), each one listens on a port. A server that binds to 127.0.0.1 can only be
reached from this PC; one that binds to 0.0.0.0, :: or a network address can be reached by every device on the same network. This check finds the listening ports of YOUR
servers and flags the ones that are not bound to the loopback address.

It looks only at processes whose command line contains one of your markers (a script name such as my_server.py), never at every listening port on the PC: operating-system
services legitimately listen on all addresses and would only be noise. The markers are yours to supply; there is no default and with none the check says so and exits 0:

  logs/listening_ports_markers.txt   one marker per line (lines starting with # are ignored)
  HARNESS_PORT_MARKERS               markers separated by commas
  --marker NAME                      on the command line (repeatable)

A marker needs at least 3 characters and at most 50 are used. A port bound beyond localhost raises one ask-first work item (kind exposed_port, tier 2, value 3) and a line in
logs/alerts.txt, once: the same exposure is not raised again while it stays, and is raised again if it goes away and comes back. Nothing is closed or killed.

It uses the optional psutil package (pip install psutil), which works on Windows, macOS and Linux. Without it, Windows falls back to PowerShell (Get-NetTCPConnection); on
macOS and Linux the check then says that psutil is needed and exits 0. A process that cannot be inspected (no permission) is counted and mentioned, not guessed about.

    python listening_ports_check.py                    # check, print, raise items
    python listening_ports_check.py --marker my_server.py
    python listening_ports_check.py --dry-run          # print only: nothing is written or raised

Every real run appends one line to logs/listening_ports_check.jsonl. Kill switch: a file logs/listening_ports_check_off.txt. The exit code is 0 whatever is found; it is 1 only
when the script itself fails. Environment: HARNESS_ROOT, HARNESS_LOGS, HARNESS_PORT_MARKERS.
Pure core (unit tested): is_exposed, match_marker, parse_markers, scan_psutil, parse_powershell."""
import argparse
import datetime
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

NAME = "listening_ports_check"
SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

MARKERS_NAME = "listening_ports_markers.txt"
ALARMED_NAME = "listening_ports_alarmed.json"
LOG_NAME = "listening_ports_check.jsonl"
OFF_NAME = "listening_ports_check_off.txt"
MIN_MARKER = 3
MAX_MARKER = 200
MAX_MARKERS = 50
MAX_OUT = 8_000_000
MAX_ALERTS = 10
PS_SCRIPT = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
             "$procs = Get-CimInstance Win32_Process | Select-Object ProcessId, CommandLine; "
             "$conns = Get-NetTCPConnection -State Listen | Select-Object LocalAddress, LocalPort, OwningProcess; "
             "[PSCustomObject]@{ Procs = $procs; Conns = $conns } | ConvertTo-Json -Depth 4")


def _logs():
    root = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
    return Path(os.environ.get("HARNESS_LOGS") or root / "logs").resolve()


def clean(text, limit=200):
    """Pure. Text that came from outside, made safe for a log line or an alert: control characters become spaces, runs of spaces collapse, cut to `limit`."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(text)).split())[:limit]


def parse_markers(*sources):
    """Pure. Each source is a list of raw strings; -> the markers: stripped, at least 3 characters, no duplicates (case-insensitive), at most 50."""
    out, seen = [], set()
    for source in sources:
        for raw in source or []:
            m = clean(raw, MAX_MARKER)
            if len(m) < MIN_MARKER or m.startswith("#") or m.lower() in seen:
                continue
            seen.add(m.lower())
            out.append(m)
    return out[:MAX_MARKERS]


def read_markers(cli=()):
    """-> the configured markers: the command line, then HARNESS_PORT_MARKERS, then logs/listening_ports_markers.txt."""
    env = (os.environ.get("HARNESS_PORT_MARKERS") or "").split(",")
    try:
        file_lines = (_logs() / MARKERS_NAME).read_text(encoding="utf-8", errors="replace")[:50000].splitlines()
    except OSError:
        file_lines = []
    return parse_markers(list(cli), env, file_lines)


def match_marker(cmdline, markers):
    """Pure. The first marker contained in the command line (case-insensitive), or None."""
    low = (cmdline or "").lower()
    for m in markers:
        if m.lower() in low:
            return m
    return None


def is_exposed(address):
    """Pure. True when a listening address can be reached from outside this PC: the wildcard addresses and every address that is not loopback. An address that cannot be read counts as exposed."""
    a = str(address or "").strip().strip("[]").split("%")[0]
    if a in ("", "*", "0.0.0.0", "::"):
        return True
    try:
        ip = ipaddress.ip_address(a)
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_loopback


def _psutil():
    """The psutil module, or None when it is not installed."""
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def scan_psutil(markers, psutil, self_pid=None):
    """-> (findings, skipped). Looks at every process whose command line holds a marker and lists its listening TCP sockets. `skipped` counts matching processes that could not be inspected."""
    self_pid = os.getpid() if self_pid is None else self_pid
    findings, seen, skipped = [], set(), 0
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            info = getattr(proc, "info", None) or {}
            pid = info.get("pid", getattr(proc, "pid", None))
            if pid == self_pid:
                continue
            marker = match_marker(" ".join(info.get("cmdline") or []), markers)
            if not marker:
                continue
            fn = getattr(proc, "net_connections", None) or getattr(proc, "connections")
            for c in fn(kind="inet"):
                if str(getattr(c, "status", "")).upper() != "LISTEN" or not getattr(c, "laddr", None):
                    continue
                addr, port = c.laddr.ip, c.laddr.port
                if (pid, addr, port) in seen:
                    continue
                seen.add((pid, addr, port))
                findings.append({"marker": marker, "address": addr, "port": port, "pid": pid, "exposed": is_exposed(addr)})
        except Exception:                                    # AccessDenied, a process that ended meanwhile, ...
            skipped += 1
    return findings, skipped


def parse_powershell(text, markers, self_pid=None):
    """Pure. -> findings from the PowerShell json (processes with command lines, listening connections). Bad text gives []."""
    self_pid = os.getpid() if self_pid is None else self_pid
    try:
        data = json.loads(text or "")
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []

    def as_list(v):
        v = v or []
        return [v] if isinstance(v, dict) else [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    owners = {}
    for p in as_list(data.get("Procs")):
        marker = match_marker(p.get("CommandLine") or "", markers)
        if marker and p.get("ProcessId") != self_pid:
            owners[p.get("ProcessId")] = marker
    findings, seen = [], set()
    for c in as_list(data.get("Conns")):
        pid = c.get("OwningProcess")
        if pid not in owners:
            continue
        addr, port = str(c.get("LocalAddress") or ""), c.get("LocalPort")
        if (pid, addr, port) in seen:
            continue
        seen.add((pid, addr, port))
        findings.append({"marker": owners[pid], "address": addr, "port": port, "pid": pid, "exposed": is_exposed(addr)})
    return findings


def run(args, timeout=60):
    """-> (exit code, stdout). 127 when the program does not exist, 124 on a time-out, 1 on any other failure. The one door for every system query; tests replace it."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout or "")[:MAX_OUT]
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""
    except Exception:
        return 1, ""


def find_listeners(markers):
    """-> {"findings": [...], "source": "psutil" | "powershell" | None, "skipped": int, "error": str}. psutil first; PowerShell only on Windows."""
    ps = _psutil()
    if ps is not None:
        findings, skipped = scan_psutil(markers, ps)
        return {"findings": findings, "source": "psutil", "skipped": skipped, "error": ""}
    if sys.platform == "win32":
        code, out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT])
        if code != 0:
            return {"findings": [], "source": "powershell", "skipped": 0, "error": "PowerShell could not list the listening ports (exit code %d)" % code}
        return {"findings": parse_powershell(out, markers), "source": "powershell", "skipped": 0, "error": ""}
    return {"findings": [], "source": None, "skipped": 0, "error": "the psutil package is needed on this system (pip install psutil)"}


def _read_json(path):
    try:
        if path.stat().st_size > MAX_OUT:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _alert(text, kind=None, tier=2, value=3, dedupe=None):
    """Tell the owner through notify.alert (alerts.txt line, optional work item). Never raises."""
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source=NAME, intent_kind=kind, tier=tier, value=value, dedupe=dedupe, logs=_logs())
    except Exception:
        try:
            lg = _logs()
            lg.mkdir(parents=True, exist_ok=True)
            with open(lg / "alerts.txt", "a", encoding="utf-8") as f:
                f.write("[%s] %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), NAME, clean(text, 500)))
        except Exception:
            pass


def _key(f):
    return hashlib.sha1(("%s|%s|%s" % (f["marker"].lower(), f["address"], f["port"])).encode("utf-8", "replace")).hexdigest()[:12]


def check(markers, dry_run=False):
    """One check. -> {"timestamp", "markers", "findings", "source", "skipped", "error"}. Writes (unless dry_run) the alarm record, one log line and the alerts."""
    logs = _logs()
    found = find_listeners(markers)
    result = dict(found, timestamp=datetime.datetime.now().isoformat(timespec="seconds"), markers=len(markers))
    if dry_run:
        return result
    exposed = [f for f in found["findings"] if f["exposed"]]
    alarmed = set(_read_json(logs / ALARMED_NAME).get("alarmed", []))
    sent = 0
    for f in exposed:
        if _key(f) in alarmed or sent >= MAX_ALERTS:
            continue
        sent += 1
        _alert("%s is listening on %s:%s (process %s), which other devices on the network can reach. Ask the owner whether it should only listen on 127.0.0.1."
               % (clean(f["marker"]), clean(f["address"], 60), f["port"], f["pid"]), kind="exposed_port", tier=2, value=3,
               dedupe="exposed-port:%s:%s" % (hashlib.sha1(f["marker"].lower().encode("utf-8", "replace")).hexdigest()[:8], f["port"]))
    now_keys = {_key(f) for f in exposed}
    if len([f for f in exposed if _key(f) not in alarmed]) > sent:
        _alert("more exposed ports were found than are listed here; see logs/%s" % LOG_NAME)
    if not found["error"] and now_keys != alarmed:
        _write_json(logs / ALARMED_NAME, {"alarmed": sorted(now_keys)})
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / LOG_NAME, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="listening_ports_check.py", description=__doc__.splitlines()[0])
    ap.add_argument("--marker", action="append", default=[], help="part of the command line of one of your servers (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="print only: write no state, log line or alert")
    args = ap.parse_args(argv)
    _utf8()
    if (_logs() / OFF_NAME).exists():
        print("listening_ports_check: off (logs/%s exists)" % OFF_NAME)
        return 0
    markers = read_markers(args.marker)
    if not markers:
        print("listening_ports_check: no markers configured; list the script names of your own servers in logs/%s (one per line) or set HARNESS_PORT_MARKERS; nothing to check" % MARKERS_NAME)
        return 0
    try:
        r = check(markers, dry_run=args.dry_run)
    except Exception as e:
        print("listening_ports_check: failed: %s: %s" % (type(e).__name__, clean(e)))
        return 1
    if r["error"]:
        print("listening_ports_check: " + r["error"])
        return 0
    exposed = [f for f in r["findings"] if f["exposed"]]
    print("=== %d listening port(s) for %d marker(s), %d bound beyond localhost ===" % (len(r["findings"]), r["markers"], len(exposed)))
    for f in r["findings"]:
        print("[%s] %s: %s:%s (pid %s)" % ("EXPOSED" if f["exposed"] else "ok", clean(f["marker"]), clean(f["address"], 60), f["port"], f["pid"]))
    if r["skipped"]:
        print("%d matching process(es) could not be inspected (no permission or already gone)" % r["skipped"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
