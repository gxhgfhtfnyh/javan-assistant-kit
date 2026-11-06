# -*- coding: utf-8 -*-
"""Hardening audit: are the computer's own protections switched on? Reads them; changes nothing.

  Windows  Defender real-time protection, firewall on every profile, UAC, drive encryption (BitLocker / device encryption, read without administrator rights)
  macOS    firewall, FileVault, Gatekeeper
  Linux    a firewall (ufw, firewalld, nftables or iptables), disk encryption (a LUKS mapping)

Each check is ok, bad or unknown. A bad check raises ONE ask-first work item (kind hardening_issue, tier 2, value 3) per check per 30 days, with a line in logs/alerts.txt; the
owner may decide a protection is not wanted (another antivirus is installed, an encrypted disk is not needed), and then the item is simply closed. Unknown is listed but raises
nothing (a check that needs administrator rights, or a tool that is not installed, is not a finding). The last result is kept in logs/hardening_baseline.json.

    python hardening_audit.py              # check, print, raise items
    python hardening_audit.py --dry-run    # check and print only: nothing is written
Paths: ROOT is the parent of the scripts folder (HARNESS_ROOT overrides), logs ROOT/logs (HARNESS_LOGS). Writes logs/sensor_last.txt (epoch seconds) on a real run."""
import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from intent_store import new_intent, is_duplicate, read_jsonl, write_intents  # noqa: E402

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent)
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs")
SENSOR_LAST = LOGS / "sensor_last.txt"
ALERTS = LOGS / "alerts.txt"
BASELINE = LOGS / "hardening_baseline.json"
RAISE_EVERY_SEC = 30 * 86400
CAVEAT = {
    "windows_defender": " (fine if another antivirus is installed)",
    "windows_firewall": " (fine if another firewall is installed)",
}
POWERSHELL = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]


def run(args, timeout=30):
    """-> (exit code, stdout). -2 when the program does not exist, -1 on a time-out, -3 when access is denied. The one door for every system query; tests replace it."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except FileNotFoundError:
        return -2, "command not found"
    except PermissionError:
        return -3, "access denied"
    except Exception as e:
        return -4, str(e)


def failed(code):
    """-> an 'unknown' result when the command itself could not run, else None."""
    if code == -2:
        return "unknown", "the tool is not installed here"
    if code == -3:
        return "unknown", "access denied"
    return None


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_baseline():
    try:
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------- Windows ----------
def check_windows_defender():
    code, out = run(POWERSHELL + ["Get-MpComputerStatus | Select-Object -ExpandProperty RealTimeProtectionEnabled"])
    if failed(code):
        return failed(code)
    if code != 0:
        return "unknown", "command failed (exit %s)" % code
    out = out.strip().lower()
    if out == "true":
        return "ok", "real-time protection enabled"
    if out == "false":
        return "bad", "real-time protection disabled"
    return "unknown", "unparseable output: " + out[:60]


def check_windows_firewall():
    code, out = run(POWERSHELL + ["Get-NetFirewallProfile | Select-Object -ExpandProperty Enabled"])
    if failed(code):
        return failed(code)
    if code != 0:
        return "unknown", "command failed (exit %s)" % code
    lines = [ln.strip().lower() for ln in out.splitlines() if ln.strip()]
    if not lines:
        return "unknown", "no output"
    if all(ln == "true" for ln in lines):
        return "ok", "firewall enabled on all %d profiles" % len(lines)
    if "false" in lines:
        return "bad", "firewall disabled on %d of %d profiles" % (lines.count("false"), len(lines))
    return "unknown", "unparseable output: " + ", ".join(lines)[:60]


def check_windows_uac():
    code, out = run(["reg", "query", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System", "/v", "EnableLUA"])
    if failed(code):
        return failed(code)
    if code != 0:
        return "unknown", "command failed (exit %s)" % code
    for line in out.splitlines():
        if "EnableLUA" in line and line.split()[-1].lower() == "0x1":
            return "ok", "UAC enabled"
        if "EnableLUA" in line and line.split()[-1].lower() == "0x0":
            return "bad", "UAC disabled"
    return "unknown", "unparseable output: " + out.strip()[:60]


def check_windows_bitlocker():
    """The shell's own BitLocker property for the system drive: 1 = protected, 0 = not protected, anything else = it cannot say (this needs no administrator rights, unlike manage-bde)."""
    code, out = run(POWERSHELL + ["(New-Object -ComObject Shell.Application).NameSpace($env:SystemDrive).Self.ExtendedProperty('System.Volume.BitLockerProtection')"])
    if failed(code):
        return failed(code)
    if code != 0:
        return "unknown", "command failed (exit %s)" % code
    value = out.strip()
    if value == "1":
        return "ok", "drive encryption protection on"
    if value == "0":
        return "bad", "drive encryption protection off"
    return "unknown", "Windows cannot say whether the system drive is encrypted"


# ---------- macOS ----------
def check_macos_firewall():
    code, out = run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    if failed(code):
        return failed(code)
    if code != 0:
        return "unknown", "command failed (exit %s)" % code
    low = out.lower()
    if "disabled" in low:                                    # checked first: "disabled" must not be read as enabled
        return "bad", "firewall disabled"
    if "enabled" in low:
        return "ok", "firewall enabled"
    return "unknown", "unparseable output: " + out.strip()[:60]


def check_macos_filevault():
    code, out = run(["fdesetup", "status"])
    if failed(code):
        return failed(code)
    if code != 0:
        return "unknown", "command failed (exit %s)" % code
    low = out.lower()
    if "filevault is on" in low:
        return "ok", "FileVault enabled"
    if "filevault is off" in low:
        return "bad", "FileVault disabled"
    return "unknown", "unparseable output: " + out.strip()[:60]


def check_macos_gatekeeper():
    code, out = run(["spctl", "--status"])
    if failed(code):
        return failed(code)
    if code != 0:
        return "unknown", "command failed (exit %s)" % code
    low = out.lower()
    if "disabled" in low:
        return "bad", "Gatekeeper disabled"
    if "enabled" in low:
        return "ok", "Gatekeeper enabled"
    return "unknown", "unparseable output: " + out.strip()[:60]


# ---------- Linux ----------
def check_linux_firewall():
    """A firewall service that is running (readable without root), else the rules themselves (which need root: then the answer is unknown, not bad)."""
    for service in ("ufw", "firewalld", "nftables"):
        code, out = run(["systemctl", "is-active", service])
        if code == 0 and out.strip() == "active":
            return "ok", "%s is active" % service
    saw_a_tool = denied = False
    code, out = run(["ufw", "status"])
    if code == 0:
        low = out.lower()
        if "status: active" in low:
            return "ok", "ufw active"
        if "status: inactive" in low:
            return "bad", "ufw inactive"
    elif code > 0:
        saw_a_tool = denied = True
    code, out = run(["nft", "list", "ruleset"])
    if code == 0:
        return ("ok", "nftables rules present") if out.strip() else ("bad", "nftables has no rules")
    saw_a_tool = saw_a_tool or code > 0
    denied = denied or code > 0
    code, out = run(["iptables", "-L", "-n"])
    if code == 0:
        rules = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith(("Chain", "target"))]
        return ("ok", "iptables rules present") if rules else ("bad", "iptables has no rules")
    saw_a_tool = saw_a_tool or code > 0
    denied = denied or code > 0
    if saw_a_tool and denied:
        return "unknown", "the firewall rules can only be read as root"
    return "unknown", "no firewall tool found (ufw, firewalld, nftables, iptables)"


def check_linux_disk_encryption():
    code, out = run(["lsblk", "-o", "TYPE", "-n"])
    if failed(code):
        return failed(code)
    if code != 0:
        return "unknown", "command failed (exit %s)" % code
    return ("ok", "an encrypted (crypt) volume is mapped") if "crypt" in out else ("bad", "no encrypted (crypt) volume found")


def get_checks_for_platform():
    system = platform.system().lower()
    if system == "windows":
        return [("windows_defender", check_windows_defender), ("windows_firewall", check_windows_firewall), ("windows_uac", check_windows_uac), ("windows_bitlocker", check_windows_bitlocker)]
    if system == "darwin":
        return [("macos_firewall", check_macos_firewall), ("macos_filevault", check_macos_filevault), ("macos_gatekeeper", check_macos_gatekeeper)]
    if system == "linux":
        return [("linux_firewall", check_linux_firewall), ("linux_disk_encryption", check_linux_disk_encryption)]
    return []


# ---------- reporting ----------
def raise_item(check_name, now=None, dry_run=False):
    """-> True when a new item was raised for this check (one per check per 30 days)."""
    now = time.time() if now is None else now
    what = "protection is off: %s%s" % (check_name, CAVEAT.get(check_name, ""))
    cand = new_intent("hardening_issue", what, tier=2, value=3, dedupe="hardening:%s:%d" % (check_name, int(now // RAISE_EVERY_SEC)), ts=now)
    rows = read_jsonl("intents.jsonl", logs=str(LOGS))
    if is_duplicate(rows, cand, now=now):
        return False
    if not dry_run:
        rows.append(cand)
        write_intents(rows, logs=str(LOGS))
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(ALERTS, "a", encoding="utf-8") as f:
            f.write("%s hardening_issue: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), what))
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check the computer's own protections.")
    ap.add_argument("--dry-run", action="store_true", help="check and print only; write nothing")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    checks = get_checks_for_platform()
    if not checks:
        print("not applicable on this system")
        return 0
    results = []
    for name, fn in checks:
        status, detail = fn()
        results.append((name, status, detail))
        print("[%s] %s: %s" % (status.upper(), name, detail))
        if status == "bad":
            raise_item(name, dry_run=args.dry_run)
    ok = sum(1 for _n, s, _d in results if s == "ok")
    bad = sum(1 for _n, s, _d in results if s == "bad")
    print("Summary: %d ok, %d bad, %d unknown" % (ok, bad, len(results) - ok - bad))
    if not args.dry_run:
        write_atomic(BASELINE, json.dumps({"last_run": time.time(), "checks": {n: {"status": s, "detail": d} for n, s, d in results}}, indent=2))
        write_atomic(SENSOR_LAST, str(int(time.time())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
