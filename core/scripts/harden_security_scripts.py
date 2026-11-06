# -*- coding: utf-8 -*-
"""
Real deletion-resistance for the critical files -- so malware or a
targeted attack can't just delete your security files. Honest framing:
nothing on Windows makes a file truly immutable against a sufficiently
privileged attacker (an admin-level process can always change ACLs before
deleting) -- but most real-world malware runs at the SAME privilege level
as the logged-in user, no escalation, and simply tries a direct delete. A
Deny ACE genuinely stops that common case outright.

Design: copies every critical file (same list `integrity_check.py`
watches -- CLAUDE.md, settings.json, every hook script) into a separate
protected backup folder, then applies an explicit Deny-Delete ACE for the
standard account on that backup folder specifically -- not on the LIVE
working files, which stay normally editable so legitimate work is never
blocked. Deny-Delete only, not Deny-Write, so this script can still
refresh the backup on a normal re-run without needing elevation every
time.

This is a second, independent layer from a git-based self-heal/restore
step elsewhere in the harness -- if the live file AND the scripts' git
history were somehow both wiped, this ACL-protected copy is a separate
fallback that doesn't depend on git surviving.

Windows-only: this applies NTFS ACLs through `icacls` and does nothing
useful on another OS.

Usage:
    python harden_security_scripts.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from integrity_check import _critical_files  # noqa: E402

# Same root convention as the rest of the harness: HARNESS_ROOT when set
# (tests, a moved install), else the folder one level up from scripts/.
PROTECTED_DIR = (Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()) / "_protected_backup"


def harden():
    PROTECTED_DIR.mkdir(exist_ok=True)
    copied = []
    for f in _critical_files():
        src = Path(f)
        if not src.exists():
            continue
        dest = PROTECTED_DIR / src.name
        shutil.copy2(src, dest)
        copied.append(str(dest))

    # Deny-Delete for the standard account, applied to the folder so it
    # covers every file inside it including ones added on a future run.
    #
    # Real bug, caught live (not just reasoned about): this originally did
    # `/inheritance:e` (ENABLE inheritance) then a plain `/deny` -- which
    # silently did nothing, because an inherited Everyone:(F) Full Control
    # grant (from the encrypted volume's own default, where this harness
    # happened to live) overrode the deny for a different-but-overlapping
    # trustee. The working fix -- break inheritance first, rebuild a clean
    # minimal ACL from scratch -- was applied by hand to the live folder at
    # the time, but this script's own source was never updated to match, so
    # it would have silently re-broken the protection on its next scheduled
    # run. A later, unrelated ACL sweep across the whole harness folder
    # (granting the owner blanket Full Control to remove a separate
    # Everyone(F) finding) re-triggered exactly this failure mode live -- a
    # real file got genuinely deleted -- which is what surfaced this gap.
    # Now matches the proven-working sequence: break inheritance, explicit
    # Modify (not Delete) for the user, explicit Full Control for SYSTEM,
    # then the deny.
    user = subprocess.run(["whoami"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["icacls", str(PROTECTED_DIR), "/inheritance:r"], capture_output=True, text=True)
    subprocess.run(["icacls", str(PROTECTED_DIR), "/grant:r", f"{user}:(OI)(CI)(M)"], capture_output=True, text=True)
    subprocess.run(["icacls", str(PROTECTED_DIR), "/grant:r", "SYSTEM:(OI)(CI)(F)"], capture_output=True, text=True)
    result = subprocess.run(
        ["icacls", str(PROTECTED_DIR), "/deny", f"{user}:(OI)(CI)(DE,DC)"],
        capture_output=True, text=True,
    )
    return {"copied": copied, "acl_result": result.stdout.strip(), "acl_ok": result.returncode == 0}


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    r = harden()
    print(f"Copied {len(r['copied'])} file(s) to {PROTECTED_DIR}")
    print(f"ACL hardening: {'OK' if r['acl_ok'] else 'FAILED'} -- {r['acl_result']}")
