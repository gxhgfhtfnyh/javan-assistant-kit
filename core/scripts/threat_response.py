# -*- coding: utf-8 -*-
"""Threat response (Windows only): spots suspicious new programs, asks Windows Defender whether they are really malware, and only acts when you switched acting on.

This is NOT an antivirus: it has no signature database. It looks at running programs that are at least 2 minutes old and new to the process baseline
(logs/process_baseline.json, written by process_baseline.py; run that first) and scores them with concrete heuristics:
    +2  running from a place malware commonly launches from (a Temp or Downloads folder)
    +3  named like a core Windows program (svchost.exe, lsass.exe, ...) but running from the wrong folder (the most common masquerade)
    +1  not digitally signed (only looked at once the first two put a program in the list; unsigned alone is far too common to flag)
A program scoring 2 or more is handed to Defender's own on-demand scanner (MpCmdRun.exe), which makes the real malware call. Three outcomes: clean, threat found,
inconclusive (not elevated, Defender missing, odd output). An inconclusive or clean scan is NEVER treated as a detection.

Default behaviour is FLAG ONLY: a log line, an alert and a work item for you. Quarantining (suspend the program, move its file to quarantine/ with a .quarantined suffix,
then end it) happens only when logs/threat_response_kill_on.txt exists AND Defender confirmed a threat AND the program is not this script's own process chain or
a file under the Windows folder. Without the switch a confirmed threat is still alerted at the highest value, with the exact action it would have taken.

    python scripts/threat_response.py [--dry-run]

--dry-run lists the candidates and what would be done, runs no scan and changes nothing. Needs psutil (pip install psutil). Defender's scan needs an elevated (administrator)
process to return a real result; run the job as administrator if you want confirmations. Off Windows it prints one line and exits 0.
Files in logs/: threat_response.jsonl (findings), threat_response_state.json (what was already reported, 24 hours). Kill switch: logs/threat_response_off.txt.
A timed job always exits 0. Pure core (unit tested): base_score, is_new, parse_scan, decide, is_signed, defender_scan, check."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

NAME = "threat_response"
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

OFF_NAME = "threat_response_off.txt"
KILL_ON_NAME = "threat_response_kill_on.txt"
STATE_NAME = "threat_response_state.json"
LOG_NAME = "threat_response.jsonl"
BASELINE_NAME = "process_baseline.json"
MIN_AGE_S = 120
NEW_WINDOW_S = 24 * 3600
SEEN_TTL_S = 24 * 3600
MIN_SCORE = 2
MAX_CANDIDATES = 20
MAX_READ = 5_000_000
MAX_LOG_BYTES = 1_000_000
SYSTEM32_PROGRAMS = ("svchost.exe", "csrss.exe", "lsass.exe", "winlogon.exe", "services.exe", "smss.exe")
SUSPICIOUS_DIRS = ("/temp/", "/downloads/")


def logs_dir():
    return Path(LOGS)


def quarantine_dir():
    return Path(ROOT) / "quarantine"


def is_windows():
    return sys.platform == "win32"


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


def norm_path(p):
    return str(p or "").replace("\\", "/").lower().rstrip("/")


def windir():
    return os.environ.get("SystemRoot") or os.environ.get("windir") or ""


def base_score(name, exe, win=None):
    """Pure. The location and masquerade heuristics -> (score, reasons). `win` is the Windows folder (default: from the environment)."""
    if not exe:
        return 0, []
    score, reasons = 0, []
    n = norm_path(exe)
    w = norm_path(win if win is not None else windir())
    if any(d in n for d in SUSPICIOUS_DIRS):
        score += 2
        reasons.append("running from a common malware-drop location (Temp/Downloads)")
    nm = (name or "").lower()
    if w and (nm in SYSTEM32_PROGRAMS or nm == "explorer.exe"):
        expected = w if nm == "explorer.exe" else w + "/system32"
        if not (n == expected or n.startswith(expected + "/")):                  # path boundary: System32Evil must not pass as System32
            score += 3
            reasons.append("named like a real Windows system process (%s) but running from the wrong path" % name)
    return score, reasons


def is_new(baseline, name, exe, now, window=NEW_WINDOW_S):
    """Pure. True when the process baseline does not know this program yet: unknown name, a path the name has not run from, or a name first seen within `window`."""
    low = {str(k).lower(): v for k, v in baseline.items()}
    key = (name or "").lower()
    entry = low.get(key) or low.get(key[:-4] if key.endswith(".exe") else key)       # process_baseline stores Windows names without .exe
    if entry is None:
        return True
    if norm_path(exe) not in {norm_path(p) for p in entry.get("paths", [])}:
        return True
    return now - float(entry.get("first_seen", 0) or 0) < window


def load_baseline(path):
    """-> (name -> entry dict, status 'missing'|'ok'|'corrupt'). Entries that do not look like {first_seen, paths: [text]} are dropped."""
    try:
        if Path(path).stat().st_size > MAX_READ:
            return {}, "corrupt"
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, ValueError):
        return {}, "corrupt"
    if not isinstance(raw, dict):
        return {}, "corrupt"
    return {k: v for k, v in raw.items() if isinstance(v, dict) and isinstance(v.get("paths"), list)}, "ok"


def _run(argv, timeout, env=None):
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                       env=dict(os.environ, **(env or {})), creationflags=0x08000000 if os.name == "nt" else 0)
    return r.returncode, (r.stdout or ""), (r.stderr or "")


def is_signed(exe, runner=None):
    """True (valid signature), False (unsigned/invalid) or None (could not tell). The path travels in an environment variable, never inside the script text."""
    try:
        code, out, _err = (runner or _run)(["powershell", "-NoProfile", "-NonInteractive", "-Command", "(Get-AuthenticodeSignature -LiteralPath $env:TR_EXE_PATH).Status"],
                                           15, {"TR_EXE_PATH": str(exe)})
    except Exception:
        return None
    out = out.strip()
    if code != 0 or not out:
        return None
    return out == "Valid"


def is_elevated():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def find_defender():
    """-> Path of MpCmdRun.exe or None. Looks under the Program Files and ProgramData folders named by the environment."""
    pf = os.environ.get("ProgramFiles")
    if pf:
        p = Path(pf) / "Windows Defender" / "MpCmdRun.exe"
        if p.is_file():
            return p
    pd = os.environ.get("ProgramData")
    if pd:
        plat = Path(pd) / "Microsoft" / "Windows Defender" / "Platform"
        try:
            for d in sorted((x for x in plat.iterdir() if x.is_dir()), key=lambda x: x.name, reverse=True):
                if (d / "MpCmdRun.exe").is_file():
                    return d / "MpCmdRun.exe"
        except OSError:
            pass
    return None


def parse_scan(output, returncode):
    """Pure. MpCmdRun's text -> 'clean' | 'threat' | 'inconclusive'. 'found no threats' / 'found 0 threats' is clean, only 'found N threats' (N >= 1) is a detection."""
    out = (output or "").lower()
    if "no threats" in out and returncode == 0:
        return "clean"
    if re.search(r"found [1-9]\d* threats?", out) or "threat(s) found" in out:
        return "threat"
    return "inconclusive"


def defender_scan(exe, cli=None, elevated=None, runner=None):
    """-> {'available', 'threat_found', 'inconclusive', ...}. Only {'threat_found': True, 'inconclusive': False} is a confirmed detection."""
    cli = cli if cli is not None else find_defender()
    if not cli:
        return {"available": False, "threat_found": False, "inconclusive": True, "reason": "Windows Defender command line not found"}
    if not (is_elevated() if elevated is None else elevated):
        return {"available": True, "threat_found": False, "inconclusive": True, "reason": "not running elevated"}
    try:
        code, out, err = (runner or _run)([str(cli), "-Scan", "-ScanType", "3", "-File", str(exe)], 120)
    except Exception as e:
        return {"available": True, "threat_found": False, "inconclusive": True, "reason": "scan error: %s" % type(e).__name__}
    verdict = parse_scan(out + err, code)
    return {"available": True, "threat_found": verdict == "threat", "inconclusive": verdict == "inconclusive", "raw": (out + err).lower()[-300:]}


def decide(scan, kill_on):
    """Pure. -> action name. Acting needs a confirmed detection AND the switch file."""
    if scan.get("threat_found") is True and scan.get("inconclusive") is False:
        return "QUARANTINE_AND_KILL" if kill_on else "THREAT_CONFIRMED_NOT_ACTED"
    if scan.get("inconclusive"):
        return "FLAGGED_ONLY_INCONCLUSIVE"
    return "FLAGGED_ONLY_CLEAN"


def protected_pids(ps):
    pids = {os.getpid()}
    try:
        pids |= {p.pid for p in ps.Process().parents()}
    except Exception:
        pass
    return pids


def can_act(pid, exe, ps, win=None):
    """-> (ok, why not). Never touches this script's own process chain, a missing file, or anything under the Windows folder."""
    if pid in protected_pids(ps):
        return False, "it is this script's own process chain"
    if not exe or not Path(exe).is_file():
        return False, "the program file is not there"
    w = norm_path(win if win is not None else windir())
    if w and (norm_path(exe) == w or norm_path(exe).startswith(w + "/")):
        return False, "it is a file under the Windows folder"
    return True, ""


def quarantine_and_kill(ps, pid, exe, created, qdir=None, now=None):
    """Suspend, move the file to quarantine/ (renamed so it cannot run), end the process. -> {'moved_to': path or None, 'killed': bool}. Checks the pid is still the same program."""
    qdir = Path(qdir or quarantine_dir())
    out = {"moved_to": None, "killed": False}
    try:
        proc = ps.Process(pid)
        if created is not None and abs(proc.create_time() - created) > 1:
            return dict(out, error="pid was reused by another program")
    except Exception as e:
        return dict(out, error="process is gone: %s" % type(e).__name__)
    try:
        proc.suspend()
    except Exception:
        pass
    try:
        qdir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(exe).name)[:80] or "program"
        dest = qdir / ("%s_%s.quarantined" % (time.strftime("%Y%m%d_%H%M%S", time.localtime(now if now is not None else time.time())), safe))
        shutil.move(str(exe), str(dest))
        out["moved_to"] = str(dest)
    except Exception:
        pass                                                           # a running program's file may be locked: still end it, and say the move failed
    try:
        proc.kill()
        out["killed"] = True
    except Exception:
        pass
    return out


def load_state(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return {k: float(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, (int, float))} if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, p)


def append_log(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".old"))
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def check(now=None, dry_run=False, ps=None, scan=None, signed=None, kill_on=None, win=None):
    """`scan(exe)` and `signed(exe)` can be replaced (tests); by default they are defender_scan and is_signed."""
    now = time.time() if now is None else now
    logs = logs_dir()
    if (logs / OFF_NAME).exists():
        return {"off": True}
    if not is_windows():
        return {"unsupported": True}
    ps = ps or _psutil()
    if ps is None:
        return {"skipped": "psutil is not installed (pip install psutil)"}
    baseline, status = load_baseline(logs / BASELINE_NAME)
    if status != "ok":
        return {"error": "no usable process baseline yet (%s): run scripts/process_baseline.py first" % status}
    kill_on = (logs / KILL_ON_NAME).exists() if kill_on is None else kill_on
    scan = scan or defender_scan
    signed = signed or is_signed
    state = {k: v for k, v in load_state(logs / STATE_NAME).items() if now - v < SEEN_TTL_S}
    cands = []
    for p in ps.process_iter(["pid", "name", "exe", "create_time"]):
        try:
            info = p.info
            exe, name = info.get("exe"), info.get("name") or ""
            if not exe or now - float(info.get("create_time") or now) < MIN_AGE_S:
                continue
            score, reasons = base_score(name, exe, win)
            if score < MIN_SCORE or not is_new(baseline, name, exe, now):
                continue
            key = hashlib.sha1((name.lower() + "|" + norm_path(exe)).encode("utf-8")).hexdigest()[:12]
            if key in state:
                continue
            cands.append({"pid": info.get("pid"), "name": name, "exe": exe, "created": info.get("create_time"), "score": score, "reasons": reasons, "key": key})
        except Exception:
            continue
        if len(cands) >= MAX_CANDIDATES:
            break
    findings = []
    for c in cands:
        entry = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), "pid": c["pid"], "name": c["name"], "exe": c["exe"], "heuristic_score": c["score"],
                 "reasons": list(c["reasons"])}
        if dry_run:
            entry["action"] = "WOULD_SCAN_WITH_DEFENDER"
            findings.append(entry)
            continue
        if signed(c["exe"]) is False:
            entry["heuristic_score"] += 1
            entry["reasons"].append("not digitally signed")
        result = scan(c["exe"])
        entry["defender_scan"] = {k: v for k, v in result.items() if k != "raw"}
        action = decide(result, kill_on)
        if action == "QUARANTINE_AND_KILL":
            ok, why = can_act(c["pid"], c["exe"], ps, win)
            if ok:
                entry["quarantine"] = quarantine_and_kill(ps, c["pid"], c["exe"], c["created"], now=now)
            else:
                action = "THREAT_CONFIRMED_NOT_ACTED"
                entry["not_acted_because"] = why
        entry["action"] = action
        findings.append(entry)
        state[c["key"]] = now
        confirmed = action in ("QUARANTINE_AND_KILL", "THREAT_CONFIRMED_NOT_ACTED")
        word = {"QUARANTINE_AND_KILL": "QUARANTINED AND ENDED", "THREAT_CONFIRMED_NOT_ACTED": "Defender CONFIRMED a threat (nothing was done: create logs/%s to allow quarantine)" % KILL_ON_NAME}.get(
            action, "suspicious, Defender %s, left running" % ("scan inconclusive" if result.get("inconclusive") else "scanned clean"))
        _alert("%s: %s (pid %s): %s; %s" % (word, c["name"], c["pid"], c["reasons"][0], "; ".join(entry["reasons"][1:3]) or "no other signs"), intent_kind="suspicious_process",
               tier=2, value=5 if confirmed else 4, dedupe="threat-" + c["key"])
    if not dry_run:
        if findings:
            append_log(logs / LOG_NAME, findings)
        save_state(logs / STATE_NAME, state)
    return {"findings": findings, "dry_run": bool(dry_run)}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog=NAME, description="score new programs, verify with Defender, act only when switched on")
    ap.add_argument("--dry-run", action="store_true", help="list candidates and the action; scan nothing, change nothing")
    a = ap.parse_args(argv)
    try:
        r = check(dry_run=a.dry_run)
    except Exception as e:
        print("[ERROR] %s: %s" % (type(e).__name__, str(e)[:120]))
        return 0
    if r.get("off"):
        print("[OFF] logs/%s exists" % OFF_NAME)
    elif r.get("unsupported"):
        print("threat_response: Windows only, nothing to do on this system")
    elif r.get("skipped"):
        print("[SKIPPED] %s" % r["skipped"])
    elif r.get("error"):
        print("[ERROR] %s" % r["error"])
    elif r["findings"]:
        for e in r["findings"]:
            print("[%s] %s (PID %s): %s" % (e.get("action", "FLAGGED"), e["name"], e["pid"], ", ".join(e["reasons"])))
    else:
        print("[OK] no new suspicious processes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
