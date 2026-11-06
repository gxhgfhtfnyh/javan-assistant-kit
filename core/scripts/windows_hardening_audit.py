# -*- coding: utf-8 -*-
"""Windows folder-permission audit: can any local user change the assistant's own files?

Windows only. Reads the access rules (with the built-in icacls tool) of the folders the assistant depends on and flags every rule that gives Everyone, the Users group or
Authenticated Users the right to write, modify or take full control. If such a rule is there, any program or account on this PC could rewrite the scripts, the boot file or
the notes, whatever else watches them by hash. A rule that DENIES access is not a risk and is ignored.

Watched folders (the ones that exist): the assistant folder (HARNESS_ROOT), the vault (HARNESS_VAULT, default ROOT/vault), ROOT/scripts and ROOT/.claude. Each finding raises
one ask-first work item (kind world_writable_folder, tier 2, value 3) and a line in logs/alerts.txt, once: the same finding is not raised again while it stays, and is
raised again if it goes away and comes back. Nothing is changed; fixing the rule is the owner's decision.

The rules are matched by their English account names ("Everyone", "BUILTIN\\Users", "NT AUTHORITY\\Authenticated Users") and by their fixed ids. On a Windows in another display
language add the local names, one per line, to logs/windows_hardening_audit_grantees.txt (lines starting with # are ignored). A folder whose rules cannot be read is listed as
unreadable; it is never reported as clean. The Run-key half of the old check is persistence_audit.py's job and is not repeated here.

    python windows_hardening_audit.py              # check, print, raise items
    python windows_hardening_audit.py --dry-run    # print only: nothing is written or raised

Every real run appends one line to logs/windows_hardening_audit.jsonl. Kill switch: a file logs/windows_hardening_audit_off.txt. Off Windows it prints one line and exits 0.
The exit code is 0 whatever is found; it is 1 only when the script itself fails. Environment: HARNESS_ROOT, HARNESS_LOGS, HARNESS_VAULT.
Pure core (unit tested): parse_rules, parse_ace, risky_rules, grantees, clean."""
import argparse
import codecs
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

NAME = "windows_hardening_audit"
SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

RISKY_GRANTEES = ("everyone", "builtin\\users", "nt authority\\authenticated users", "authenticated users", "*s-1-1-0", "*s-1-5-32-545", "*s-1-5-11")
RISKY_RIGHTS = ("F", "M", "W", "GA", "GW", "WD", "AD")      # icacls shorthand: full control, modify, write, generic all/write, write data / add file, append data / add folder
GRANTEES_NAME = "windows_hardening_audit_grantees.txt"
ALARMED_NAME = "windows_hardening_alarmed.json"
LOG_NAME = "windows_hardening_audit.jsonl"
OFF_NAME = "windows_hardening_audit_off.txt"
ACE_RE = re.compile(r"^(?P<account>.+?):(?P<groups>(?:\([^)]*\))+)\s*$")
MAX_OUT = 1_000_000
MAX_ALERTS = 10


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


def _vault():
    return Path(os.environ.get("HARNESS_VAULT") or _root() / "vault").resolve()


def watched_folders():
    """-> [(label, folder)] for the watched folders that exist, without duplicates."""
    root = _root()
    wanted = [("assistant folder", root), ("vault", _vault()), ("scripts", root / "scripts"), ("settings", root / ".claude")]
    out, seen = [], set()
    for label, folder in wanted:
        key = str(folder).lower()
        if key in seen or not folder.is_dir():
            continue
        seen.add(key)
        out.append((label, folder))
    return out


def _oem():
    """icacls prints in the console's OEM code page; Python knows it as 'oem' on Windows."""
    try:
        codecs.lookup("oem")
        return "oem" if os.name == "nt" else "utf-8"
    except LookupError:
        return "utf-8"


def run(args, timeout=20):
    """-> (exit code, stdout). 127 when the program does not exist, 124 on a time-out, 1 on any other failure. The one door for every system query; tests replace it."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, encoding=_oem(), errors="replace", stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout or "")[:MAX_OUT]
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""
    except Exception:
        return 1, ""


def clean(text, limit=200):
    """Pure. Text that came from outside, made safe for a log line or an alert: control characters become spaces, runs of spaces collapse, cut to `limit`."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(text)).split())[:limit]


def parse_rules(text, path=""):
    """Pure. The access rules in icacls output: the lines before the first blank line (the summary after it is in the display language), with the folder name that icacls puts
    in front of the first rule removed."""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            if lines:
                break
            continue
        lines.append(line)
    if lines and path and lines[0].lower().startswith(str(path).lower()):
        lines[0] = lines[0][len(str(path)):].strip()
    return [" ".join(l.split()) for l in lines if l.strip()]


def parse_ace(line):
    """Pure. 'BUILTIN\\Users:(OI)(CI)(M)' -> ('builtin\\users', {'OI', 'CI', 'M'}); None when the line is not a rule."""
    m = ACE_RE.match(line.strip())
    if not m:
        return None
    tokens = set()
    for group in re.findall(r"\(([^)]*)\)", m.group("groups")):
        tokens.update(t.strip().upper() for t in group.split(",") if t.strip())
    return m.group("account").strip().lower(), tokens


def grantees(extra=()):
    """Pure. The account names (lower case) whose write access counts as a risk."""
    return set(RISKY_GRANTEES) | {e.strip().lower() for e in extra if e and e.strip()}


def risky_rules(rules, extra=()):
    """Pure. The rules that give a broad group write, modify or full control. A DENY rule is never risky."""
    names = grantees(extra)
    out = []
    for rule in rules:
        parsed = parse_ace(rule)
        if not parsed:
            continue
        account, tokens = parsed
        if "DENY" in tokens:
            continue
        if account in names and tokens & set(RISKY_RIGHTS):
            out.append(rule)
    return out


def read_extra_grantees():
    """The local-language group names the owner added in logs/windows_hardening_audit_grantees.txt (at most 20 lines)."""
    try:
        text = (_logs() / GRANTEES_NAME).read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        return []
    return [l.strip()[:100] for l in text.splitlines() if l.strip() and not l.strip().startswith("#")][:20]


def scan(folders, extra=()):
    """-> (findings, unreadable). findings = [{"folder": label, "rule": rule}]; unreadable = [labels whose rules could not be read]."""
    findings, unreadable = [], []
    for label, folder in folders:
        code, out = run(["icacls", str(folder)])
        rules = parse_rules(out, folder) if code == 0 else []
        if code != 0 or not rules:                           # a failed icacls with empty output must not read as a clean folder
            unreadable.append(label)
            continue
        findings.extend({"folder": label, "rule": r} for r in risky_rules(rules, extra))
    return findings, unreadable


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


def _finding_key(f):
    return hashlib.sha1(("%s|%s" % (f["folder"], f["rule"])).encode("utf-8", "replace")).hexdigest()[:12]


def check(dry_run=False):
    """One audit. -> {"timestamp", "checked", "findings", "unreadable"}. Writes (unless dry_run) the alarm record, one log line and the alerts."""
    logs = _logs()
    folders = watched_folders()
    findings, unreadable = scan(folders, read_extra_grantees())
    result = {"timestamp": datetime.datetime.now().isoformat(timespec="seconds"), "checked": [l for l, _ in folders], "findings": findings, "unreadable": unreadable}
    if dry_run:
        return result
    alarmed = set(_read_json(logs / ALARMED_NAME).get("alarmed", []))
    now_keys = {_finding_key(f) for f in findings}
    sent = 0
    for f in findings:
        key = _finding_key(f)
        if key in alarmed:
            continue
        if sent >= MAX_ALERTS:
            break
        sent += 1
        _alert("the %s folder lets a broad group change files (%s). Ask the owner whether to tighten it." % (f["folder"], clean(f["rule"])),
               kind="world_writable_folder", tier=2, value=3, dedupe="worldwritable:" + key)
    pending = [f for f in findings if _finding_key(f) not in alarmed]
    if len(pending) > sent:
        _alert("%d more folder-permission finding(s) not listed here; see logs/%s" % (len(pending) - sent, LOG_NAME))
    if now_keys != alarmed:
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
    ap = argparse.ArgumentParser(prog="windows_hardening_audit.py", description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print only: write no state, log line or alert")
    args = ap.parse_args(argv)
    _utf8()
    if (_logs() / OFF_NAME).exists():
        print("windows_hardening_audit: off (logs/%s exists)" % OFF_NAME)
        return 0
    if sys.platform != "win32":
        print("windows_hardening_audit: Windows only (it reads folder permissions with icacls); nothing to do on this system")
        return 0
    try:
        r = check(dry_run=args.dry_run)
    except Exception as e:
        print("windows_hardening_audit: failed: %s: %s" % (type(e).__name__, clean(e)))
        return 1
    for label in r["unreadable"]:
        print("[UNREADABLE] the access rules of the %s folder could not be read; not checked" % label)
    if r["findings"]:
        print("[ACL RISK] %d finding(s):" % len(r["findings"]))
        for f in r["findings"]:
            print("  - %s: %s" % (f["folder"], clean(f["rule"], 300)))
    elif r["checked"] and not r["unreadable"]:
        print("[ACL OK] no broad write access on: %s" % ", ".join(r["checked"]))
    elif not r["checked"]:
        print("[NOTHING TO CHECK] none of the watched folders exist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
