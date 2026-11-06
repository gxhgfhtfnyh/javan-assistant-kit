# -*- coding: utf-8 -*-
"""Local binding check: warns when a server on this computer listens on every network interface (0.0.0.0, ::) or on the LAN address instead of on localhost only.

A development server or a local tool started with a "listen on everything" option by accident is reachable by anyone on the same network. Two rules, both read-only:
  1. Watch list: logs/local_binding_watch.txt, one line per port that must stay localhost-only ("8188 my image tool"; '#' starts a comment). An exposed watched port
     alerts on every run. The file does not exist by default; you add the ports of the local tools you run.
  2. Anything else: the first run records every exposed TCP listener as the baseline (printed, so you can read it); later a NEW exposed listener alerts once.
     `--accept` adds what is listening now to the baseline after you have looked at it.

    python scripts/local_binding_check.py [--dry-run] [--accept]

Listeners come from psutil when it is installed (pip install psutil), otherwise from `ss` (Linux) or `netstat` (Windows, macOS), so it works without extra packages.
Files in logs/: local_binding_baseline.json, local_binding_check.jsonl, local_binding_watch.txt. Kill switch: logs/local_binding_check_off.txt. --dry-run changes nothing.
A timed job always exits 0; findings go through notify.alert.
Pure core (unit tested): parse_host_port, parse_listeners, is_exposed, read_watch, evaluate, check."""
import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

NAME = "local_binding_check"
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

BASELINE_NAME = "local_binding_baseline.json"
WATCH_NAME = "local_binding_watch.txt"
LOG_NAME = "local_binding_check.jsonl"
OFF_NAME = "local_binding_check_off.txt"
MAX_READ = 2_000_000
MAX_LOG_BYTES = 1_000_000
MAX_PORTS = 5000


def logs_dir():
    return Path(LOGS)


def _alert(text, **kw):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        return notify.alert(text, source=NAME, logs=str(logs_dir()), **kw)
    except Exception:
        return None


def parse_host_port(token):
    """'127.0.0.1:8188', '[::1]:8188', ':::22', '*:80', '*.5000' (macOS), '::1.5000' (macOS) -> (host, port) or None."""
    t = token.strip()
    if not t or t.endswith(":*") or t.endswith(".*"):
        return None
    if t.startswith("[") and "]" in t:
        host, _, rest = t[1:].partition("]")
        port = rest.lstrip(":.")
    elif t.count(":") == 1:
        host, _, port = t.rpartition(":")
    elif t.count(":") > 1:
        i, j = t.rfind("."), t.rfind(":")
        host, port = (t[:i], t[i + 1:]) if i > j else (t[:j], t[j + 1:])
    else:
        host, _, port = t.rpartition(".")
    if not port.isdigit() or not 0 < int(port) < 65536:
        return None
    return host.split("%")[0], int(port)


def parse_listeners(text):
    """Pure. The (host, port) of every LISTEN line of `ss -ltn`, `netstat -an` (Windows, macOS) or `netstat -ltn`. Lines that are not listeners are ignored."""
    out = []
    for line in str(text or "").splitlines():
        if "LISTEN" not in line.upper():
            continue
        toks = line.split()
        for tok in toks:
            if tok.isdigit():
                continue
            hp = parse_host_port(tok)
            if hp:
                out.append(hp)
                break
    return out


def is_exposed(host):
    """True when a listener on `host` can be reached from another computer: every-interface wildcards and any non-loopback address."""
    h = (host or "").strip().lower()
    if h in ("localhost", "ip6-localhost"):
        return False
    if h in ("", "*", "0.0.0.0", "::"):
        return True
    try:
        a = ipaddress.ip_address(h)
    except ValueError:
        return True
    if getattr(a, "ipv4_mapped", None) is not None:
        a = a.ipv4_mapped
    return not a.is_loopback


def read_watch(logs=None):
    """-> {port: label} from logs/local_binding_watch.txt. Lines that are not '<port> [label]' are ignored."""
    try:
        text = (Path(logs or logs_dir()) / WATCH_NAME).read_text(encoding="utf-8", errors="replace")[:MAX_READ]
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        mt = re.match(r"^\s*(\d{1,5})\b\s*(.*)$", line.split("#", 1)[0])
        if mt and 0 < int(mt.group(1)) < 65536:
            out[int(mt.group(1))] = mt.group(2).strip()[:60] or "port %s" % mt.group(1)
    return out


def listeners_psutil(ps):
    """-> [(host, port, program name)] or None when psutil cannot list sockets here (macOS without root raises AccessDenied)."""
    try:
        out = []
        for c in ps.net_connections(kind="inet"):
            if c.status == ps.CONN_LISTEN and c.laddr:
                name = ""
                if getattr(c, "pid", None):
                    try:
                        name = ps.Process(c.pid).name()
                    except Exception:
                        name = ""
                out.append((c.laddr.ip, c.laddr.port, name))
        return out
    except Exception:
        return None


def _command():
    if sys.platform == "win32":
        return ["netstat", "-an"]
    if sys.platform == "darwin":
        return ["netstat", "-an", "-p", "tcp"]
    if shutil.which("ss"):
        return ["ss", "-H", "-ltn"]
    return ["netstat", "-ltn"]


def run_command(argv):
    """Run a fixed read-only program. -> its output text, or None when it is missing, fails or hangs."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return (r.stdout or "")[:MAX_READ] if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def gather(ps=None, runner=None):
    """-> ([(host, port, program)], source) with source 'psutil', 'command' or None when nothing could list the sockets."""
    ps = ps if ps is not None else _psutil()
    if ps is not None:
        got = listeners_psutil(ps)
        if got is not None:
            return got, "psutil"
    text = (runner or run_command)(_command())
    if text is None:
        return [], None
    return [(h, p, "") for h, p in parse_listeners(text)], "command"


def _psutil():
    try:
        import psutil
        return psutil
    except Exception:
        return None


def load_baseline(path):
    """-> (set of ports or None, status 'missing'|'ok'|'corrupt')."""
    try:
        if Path(path).stat().st_size > MAX_READ:
            return None, "corrupt"
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "corrupt"
    ports = raw.get("ports") if isinstance(raw, dict) else None
    if not isinstance(ports, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in ports):
        return None, "corrupt"
    return set(ports), "ok"


def save_baseline(path, ports):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps({"ports": sorted(ports)[:MAX_PORTS]}, indent=2), encoding="utf-8")
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


def evaluate(listeners, watch, baseline):
    """Pure. -> (watched_hits, new_exposed, exposed). exposed = {port: {'hosts': [...], 'program': name}} for every listener not bound to localhost only.
    watched_hits: exposed ports on the watch list. new_exposed: exposed ports not in the baseline and not already counted as watched (empty on the first run, baseline None)."""
    exposed = {}
    for host, port, prog in listeners:
        if is_exposed(host):
            e = exposed.setdefault(port, {"hosts": [], "program": ""})
            if host not in e["hosts"]:
                e["hosts"].append(host)
            e["program"] = e["program"] or prog
    watched = sorted(p for p in exposed if p in watch)
    new = [] if baseline is None else sorted(p for p in exposed if p not in baseline and p not in watch)
    return watched, new, exposed


def _describe(port, e, label=""):
    prog = " (%s)" % e["program"] if e.get("program") else ""
    return "%sport %d%s bound to %s" % (label + " " if label else "", port, prog, ", ".join(e["hosts"][:3]))


def check(dry_run=False, accept=False, ps=None, runner=None, now=None):
    now = time.time() if now is None else now
    logs = logs_dir()
    if (logs / OFF_NAME).exists():
        return {"off": True}
    listeners, source = gather(ps, runner)
    if source is None:
        return {"skipped": "cannot list listening sockets here (no psutil, and ss/netstat failed)"}
    watch = read_watch(logs)
    base_path = logs / BASELINE_NAME
    baseline, status = load_baseline(base_path)
    if status == "corrupt":
        if not dry_run:
            try:
                os.replace(base_path, base_path.with_name(base_path.name + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(now))))
            except OSError:
                pass
        baseline = None
    first_run = baseline is None
    watched, new, exposed = evaluate(listeners, watch, baseline)
    findings = [_describe(p, exposed[p], "watched (%s)" % watch[p]) for p in watched] + [_describe(p, exposed[p], "new exposed listener:") for p in new]
    result = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), "source": source, "first_run": first_run, "findings": findings,
              "exposed_ports": sorted(exposed), "listening": len(listeners), "dry_run": bool(dry_run)}
    if dry_run:
        return result
    if first_run or accept:
        save_baseline(base_path, (baseline or set()) | set(exposed))
    append_log(logs / LOG_NAME, result)
    if watched:
        _alert("a server you listed as localhost-only is exposed: " + "; ".join(findings[:len(watched)][:3]), intent_kind="exposed_server", tier=2, value=4,
               dedupe="localbind-watch-" + "-".join(map(str, watched)))
    if new and not accept:
        _alert("%d new server port(s) listening beyond localhost: %s" % (len(new), "; ".join(findings[len(watched):][:3])), intent_kind="exposed_server", tier=2, value=3,
               dedupe="localbind-new-" + "-".join(map(str, new)))
    return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog=NAME, description="warn when a local server listens beyond localhost")
    ap.add_argument("--dry-run", action="store_true", help="change nothing: no baseline, log or alert")
    ap.add_argument("--accept", action="store_true", help="add everything listening now to the baseline (after you have looked at it)")
    a = ap.parse_args(argv)
    try:
        r = check(dry_run=a.dry_run, accept=a.accept)
    except Exception as e:
        print("[ERROR] %s: %s" % (type(e).__name__, str(e)[:120]))
        return 0
    if r.get("off"):
        print("[OFF] logs/%s exists" % OFF_NAME)
    elif r.get("skipped"):
        print("[SKIPPED] %s" % r["skipped"])
    elif r["findings"]:
        print("[EXPOSED] %s" % r["findings"])
    elif r["first_run"]:
        print("[BASELINE_SET] exposed listener ports: %s" % (r["exposed_ports"] or "none"))
    else:
        print("[OK] nothing new beyond localhost (exposed ports: %s)" % (r["exposed_ports"] or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
