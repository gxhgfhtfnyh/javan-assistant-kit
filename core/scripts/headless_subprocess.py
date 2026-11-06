# -*- coding: utf-8 -*-
"""Keeps every program a background script starts from opening a visible console window (Windows only; on other systems it does nothing).

Why: a scheduled task runs pythonw.exe, which has no console. When such a script starts a console program (python.exe, powershell, git, schtasks, claude, nvidia-smi ...)
without the CREATE_NO_WINDOW flag, Windows opens a new console window for the child for a moment. Every script that starts a child has to remember the flag; one that forgot it is a flash that steals focus (even from a fullscreen game).

What it does: patch() wraps subprocess.Popen.__init__ so that a child started without any creationflags gets CREATE_NO_WINDOW, but only when THIS process has no console window
(a background task); a terminal session you are typing in is left alone, and an explicit creationflags value always wins. It also routes os.system through subprocess, because
os.system opens a console on its own. A GUI program started this way still shows its window normally (the flag only affects console programs).
    python headless_subprocess.py --install     # copy this file to the user site as usercustomize.py, so every Python of this user applies it at startup (a backup of a different
                                                # existing usercustomize.py is kept next to it)
    python headless_subprocess.py --status      # shows whether it is installed and whether it is active in this process
    python headless_subprocess.py --uninstall   # removes the installed copy (and restores the backup if there was one)
Pure core (unit tested): patch(popen, has_console, system_owner), install(target_dir, source), uninstall(target_dir), status(target_dir)."""
import os
import shutil
import subprocess
import sys

NO_WINDOW = 0x08000000
MARK = "_headless_patched"
NAME = "usercustomize.py"


def has_console():
    """True when this process has a console window (a terminal you can see); False for pythonw and scheduled tasks."""
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return False


def patch(popen=None, console=has_console, system_owner=None):
    """Wrap Popen.__init__ (and os.system) once. -> True when the patch was applied now, False when it was already applied or the process has a console."""
    popen = popen or subprocess.Popen
    if getattr(popen.__init__, MARK, False) or console():
        return False
    original = popen.__init__

    def init(self, *args, **kwargs):
        # creationflags is the 14th positional parameter of Popen; a caller that passes it positionally, or passes any non-zero value, chose for itself
        if len(args) < 14 and not kwargs.get("creationflags"):
            kwargs["creationflags"] = NO_WINDOW
        original(self, *args, **kwargs)

    setattr(init, MARK, True)
    popen.__init__ = init
    owner = system_owner if system_owner is not None else os
    if not getattr(owner.system, MARK, False):
        def system(command):
            return popen(command, shell=True).wait()

        setattr(system, MARK, True)
        owner.system = system
    return True


def install(target_dir, source=None):
    """Copy this file to target_dir/usercustomize.py. -> a short message. A different file already there is kept as usercustomize.py.bak_headless first."""
    source = source or os.path.abspath(__file__)
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, NAME)
    with open(source, "rb") as f:
        new = f.read()
    if os.path.exists(target):
        with open(target, "rb") as f:
            old = f.read()
        if old == new:
            return "already installed"
        shutil.copy2(target, target + ".bak_headless")
        msg = "installed (the previous usercustomize.py is kept as %s.bak_headless)" % NAME
    else:
        msg = "installed"
    with open(target, "wb") as f:
        f.write(new)
    return msg


def uninstall(target_dir):
    target = os.path.join(target_dir, NAME)
    backup = target + ".bak_headless"
    if not os.path.exists(target):
        return "not installed"
    os.remove(target)
    if os.path.exists(backup):
        os.replace(backup, target)
        return "removed; the previous usercustomize.py is restored"
    return "removed"


def status(target_dir):
    target = os.path.join(target_dir, NAME)
    installed = os.path.exists(target)
    active = bool(getattr(subprocess.Popen.__init__, MARK, False))
    return "installed: %s; active in this process: %s (it only activates in a process without a console window)" % ("yes" if installed else "no", "yes" if active else "no")


def user_site():
    import site
    return site.getusersitepackages()


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if "--install" in argv:
        print(install(user_site()))
    elif "--uninstall" in argv:
        print(uninstall(user_site()))
    elif "--status" in argv:
        print(status(user_site()))
    else:
        print(__doc__.strip().splitlines()[0])
        return 2
    return 0


if os.name == "nt" and __name__ == "usercustomize":
    try:                      # loaded as usercustomize.py this runs at every Python start; it must never be able to stop one
        patch()
    except Exception:
        pass

if __name__ == "__main__":
    sys.exit(main())
