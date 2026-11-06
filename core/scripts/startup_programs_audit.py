# -*- coding: utf-8 -*-
"""Startup programs audit: what is set to start by itself when this Windows account logs in?

Windows only. Lists the autostart entries a person can read without administrator rights and prints them. It is a list, not a watcher: it keeps no baseline, writes no
file and raises no alert (persistence_audit.py is the one that remembers a list and raises an item when it changes; it covers the 64-bit Run keys, the Startup folders
and scheduled tasks). This tool adds the places that one does not look at:

  Registry   Run and RunOnce for the current user; Run and RunOnce for the machine (64-bit view); the machine's Run key in the 32-bit view (WOW6432Node); the
             Policies\\Explorer\\Run keys, which Windows runs like Run but most tools forget
  Folders    the Startup folder of the current user and the one for all users

An entry that starts from a temporary or download folder, or through a script host (PowerShell, wscript, mshta, ...) or a web address, is marked "worth a look". That is a
pointer, not proof: plenty of honest programs use a script host. The command lines are printed exactly as stored, so they can hold secrets; nothing is logged.

    python startup_programs_audit.py            # print the list
    python startup_programs_audit.py --json     # the same as json

Off Windows (or without the winreg module) it prints one line and exits 0. The exit code is 0 unless the script itself fails.
Pure core (unit tested): worth_a_look, format_item, folders. Reading the registry goes through list_startup_items, which tests feed with a fake winreg."""
import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import winreg
except ImportError:                      # not Windows: the script still imports; main() says so and exits 0
    winreg = None

NAME = "startup_programs_audit"
RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_ONCE = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
POLICY_RUN = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run"
# (label, hive attribute of winreg, subkey, registry view attribute of winreg or None)
REGISTRY_PLACES = [
    ("HKCU Run", "HKEY_CURRENT_USER", RUN, None),
    ("HKCU RunOnce", "HKEY_CURRENT_USER", RUN_ONCE, None),
    ("HKCU Policies Run", "HKEY_CURRENT_USER", POLICY_RUN, None),
    ("HKLM Run", "HKEY_LOCAL_MACHINE", RUN, "KEY_WOW64_64KEY"),
    ("HKLM RunOnce", "HKEY_LOCAL_MACHINE", RUN_ONCE, "KEY_WOW64_64KEY"),
    ("HKLM Run (32-bit)", "HKEY_LOCAL_MACHINE", RUN, "KEY_WOW64_32KEY"),
    ("HKLM Policies Run", "HKEY_LOCAL_MACHINE", POLICY_RUN, "KEY_WOW64_64KEY"),
]
STARTUP_SUB = Path("Microsoft") / "Windows" / "Start Menu" / "Programs" / "Startup"
MAX_NAME = 200
MAX_COMMAND = 500
MAX_ENTRIES_PER_PLACE = 500

TEMP_MARKERS = ("\\temp\\", "%temp%", "%tmp%", "\\downloads\\")          # matched against the command with / turned into \
HOST_MARKERS = ("powershell", "pwsh", "wscript", "cscript", "mshta", "cmd /c", "cmd.exe /c", "regsvr32", "-encodedcommand", " -enc ", "http://", "https://")


def worth_a_look(command):
    """Pure. A short reason when the command runs from a temporary or download folder, or through a script host or a web address; otherwise ''."""
    low = (command or "").lower()
    if any(m in low.replace("/", "\\") for m in TEMP_MARKERS):
        return "starts from a temporary or download folder"
    if any(m in low for m in HOST_MARKERS):
        return "starts through a script host or a web address"
    return ""


def folders():
    """-> [(label, Path)]: the Startup folders to list, from the environment at call time."""
    out = []
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    out.append(("Startup folder (this user)", base / STARTUP_SUB))
    programdata = os.environ.get("PROGRAMDATA")
    if programdata:
        out.append(("Startup folder (all users)", Path(programdata) / STARTUP_SUB))
    return out


def _registry_items():
    items = []
    for label, hive_attr, subkey, view_attr in REGISTRY_PLACES:
        try:
            hive = getattr(winreg, hive_attr)
            access = winreg.KEY_READ | (getattr(winreg, view_attr, 0) if view_attr else 0)
            with winreg.OpenKey(hive, subkey, 0, access) as key:
                for i in range(MAX_ENTRIES_PER_PLACE):
                    try:
                        name, value, _kind = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    items.append({"source": label, "name": str(name)[:MAX_NAME] or "(default)", "command": str(value)[:MAX_COMMAND]})
        except OSError:
            continue                                         # the key does not exist or cannot be opened: nothing to list
    return items


def _folder_items():
    items = []
    for label, folder in folders():
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if name.lower() == "desktop.ini":
                continue
            items.append({"source": label, "name": name[:MAX_NAME], "command": str(folder / name)[:MAX_COMMAND]})
    return items


def list_startup_items():
    """-> [{"source", "name", "command", "look"}]. "look" is the worth_a_look reason or ''."""
    items = (_registry_items() if winreg is not None else []) + _folder_items()
    for it in items:
        it["look"] = worth_a_look(it["command"])
    return items


def format_item(item):
    """Pure. One printable line for an entry; control characters in a name or command are replaced so a stored value cannot move the cursor or fake a line."""
    safe = lambda s: re.sub(r"[\x00-\x1f\x7f]", "?", str(s))
    line = "[%s] %s: %s" % (safe(item["source"]), safe(item["name"]), safe(item["command"]))
    if item.get("look"):
        line += "   <- worth a look: " + item["look"]
    return line


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="startup_programs_audit.py", description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="print the list as json")
    args = ap.parse_args(argv)
    _utf8()
    if sys.platform != "win32" or winreg is None:
        print("startup_programs_audit: Windows only (Registry Run keys and the Startup folder); on this system persistence_audit.py lists what starts by itself")
        return 0
    try:
        items = list_startup_items()
    except Exception as e:
        print("startup_programs_audit: failed: %s" % type(e).__name__)
        return 1
    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return 0
    flagged = [i for i in items if i["look"]]
    print("=== %d startup item(s), %d worth a look ===\n" % (len(items), len(flagged)))
    for it in items:
        print(format_item(it))
    return 0


if __name__ == "__main__":
    sys.exit(main())
