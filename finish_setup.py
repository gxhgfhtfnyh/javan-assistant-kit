# -*- coding: utf-8 -*-
"""Finishes the setup after the vault and scripts are generated, with no manual steps left for the person (one yes to "shall I build and set everything up?" covers all of it).

    python finish_setup.py --out <assistant folder>            # prints the plan only, runs nothing
    python finish_setup.py --out <assistant folder> --yes      # does it
    options: --skip step,step   --only step,step   --force (run steps that already finished)   --status (show the last run)
    python finish_setup.py --trust-only <folder>               # only mark a folder as trusted in Claude Code (the start scripts use this for the kit folder)
Steps, in order (each is idempotent and one failing step does not stop the others; a step that finished is not repeated on a second run unless --force):
  pytest      install pytest if it is missing (the parity test needs it)
  headless    Windows only: make background Python jobs start their child programs without a console window (one small file in the Python user folder; undo with scripts/headless_subprocess.py --uninstall)
  obsidian    install Obsidian when it is missing (winget on Windows, Homebrew on macOS, Flatpak on Linux)
  vault       register the vault folder with Obsidian so it opens there (Obsidian's own obsidian.json; the old file is backed up first)
  scheduler   register the job runner with the OS scheduler (every 5 minutes) and check that it really ran once
  parity      run the seven end-to-end checks on a copy of the new folder
  trust       mark the new folder as trusted in Claude Code's settings, so the first start does not ask (the old file is backed up first)
  launch      open the vault in Obsidian and open a terminal in the new folder that starts the assistant
Exit code 0 when no step failed ("manual" means the step could not be automated on this machine and says what to do), 1 when a step failed, 2 for wrong usage.
Every step line is printed as soon as the step ends, the whole report is also written to <folder>/logs/finish_setup.txt, and the state of each step to logs/finish_setup.json.
Pure core (unit tested): plan, run_steps and every step function (they take a Context whose runner, opener and launcher can be replaced)."""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
FIRST_PROMPT = "Introduce yourself in two short sentences and ask what is on the list today."
WINGET_OK_TEXT = ("already installed", "no applicable upgrade", "no newer package")
SHELL_EXECUTE = "@shellexecute"                                              # marker in a launch argv: [marker, program, parameters, working folder] is opened through the Windows shell


class Context:
    def __init__(self, out, platform=None, runner=None, home=None, opener=None, which=None, python=None, launcher=None, env=None):
        self.out = os.path.abspath(out)
        self.platform = platform or sys.platform
        self.runner = runner or self._run
        self.home = home or os.path.expanduser("~")
        self.opener = opener or self._open
        self.launcher = launcher or self._launch
        self.which = which or shutil.which
        self.python = python or sys.executable
        self.env = os.environ if env is None else env

    @staticmethod
    def _run(args, timeout=900):
        flags = 0x08000000 if os.name == "nt" else 0
        r = subprocess.run(args, shell=isinstance(args, str), capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags,
                           stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()

    @staticmethod
    def _open(target):
        if sys.platform.startswith("win"):
            os.startfile(target)                                           # noqa: S606 - opens a file, folder or obsidian:// address with its registered app
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    @staticmethod
    def _launch(argv):
        """Start a program that stays open (a terminal window) WITHOUT waiting for it and without sharing our pipes, which would make the caller wait until the window is closed.
        -> (ok, detail): ok when it started and did not fail at once."""
        if argv[0] == SHELL_EXECUTE:
            # Windows: ask the shell to open the program, as a double-click would. A program started with redirected or inherited standard handles
            # (a pipe, or NUL) gets those same handles in its new window: with NUL, `cmd /k` reads end-of-file and closes at once.
            import ctypes
            code = ctypes.windll.shell32.ShellExecuteW(None, "open", argv[1], argv[2], argv[3], 1)
            return code > 32, "started" if code > 32 else "the shell could not open %s (code %d)" % (argv[1], code)
        extra = {"creationflags": 0x00000200 | 0x00000008} if os.name == "nt" else {"start_new_session": True}      # own process group, detached
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **extra)
        except OSError as e:
            return False, "could not start %s: %s" % (argv[0], e)
        try:
            code = p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            return True, "started"                                          # still running after a few seconds: the terminal is up
        return code == 0, "started" if code == 0 else "%s exited with code %s" % (argv[0], code)

    @property
    def windows(self):
        return self.platform.startswith("win")

    @property
    def mac(self):
        return self.platform == "darwin"

    @property
    def vault(self):
        return os.path.join(self.out, "vault")

    def script(self, name):
        return os.path.join(self.out, "scripts", name)


def result(name, status, detail=""):
    return {"name": name, "status": status, "detail": detail}


# ---------- steps ----------
def step_pytest(ctx):
    code, _out = ctx.runner([ctx.python, "-c", "import pytest"])
    if code == 0:
        return result("pytest", "skipped", "already installed")
    code, out = ctx.runner([ctx.python, "-m", "pip", "install", "pytest"])
    if code != 0:                                                           # a user-level install works where the system one is locked
        code, out2 = ctx.runner([ctx.python, "-m", "pip", "install", "--user", "pytest"])
        out = out + "\n" + out2 if code != 0 else out
    if code != 0 and "externally-managed" in out:                           # Debian, Ubuntu, Fedora and Homebrew Python refuse pip without this flag
        code, out3 = ctx.runner([ctx.python, "-m", "pip", "install", "--user", "--break-system-packages", "pytest"])
        out = out + "\n" + out3 if code != 0 else out
    return result("pytest", "done" if code == 0 else "failed", "installed with pip" if code == 0 else "pip install pytest failed: " + out[-400:])


def step_headless(ctx):
    if not ctx.windows:
        return result("headless", "skipped", "not needed on this system")
    code, out = ctx.runner([ctx.python, ctx.script("headless_subprocess.py"), "--install"])
    return result("headless", "done" if code == 0 else "failed", out[-300:])


def obsidian_paths(ctx):
    """Where an installed Obsidian can be, as a list of files or folders to look for."""
    env = ctx.env
    if ctx.windows:
        out = []
        for var, rel in (("LOCALAPPDATA", ("Programs", "Obsidian", "Obsidian.exe")), ("LOCALAPPDATA", ("Obsidian", "Obsidian.exe")),
                         ("ProgramFiles", ("Obsidian", "Obsidian.exe")), ("ProgramFiles(x86)", ("Obsidian", "Obsidian.exe"))):
            if env.get(var):
                out.append(os.path.join(env[var], *rel))
        return out
    if ctx.mac:
        return ["/Applications/Obsidian.app", os.path.join(ctx.home, "Applications", "Obsidian.app")]
    return []


def obsidian_installed(ctx):
    if any(os.path.exists(p) for p in obsidian_paths(ctx)):
        return True
    if ctx.which("obsidian"):
        return True
    if not ctx.windows and not ctx.mac and ctx.which("flatpak"):
        code, out = ctx.runner(["flatpak", "list", "--app"])
        return code == 0 and "md.obsidian.Obsidian" in out
    return False


def step_obsidian(ctx):
    if obsidian_installed(ctx):
        return result("obsidian", "skipped", "already installed")
    if ctx.windows and ctx.which("winget"):
        cmd = ["winget", "install", "-e", "--id", "Obsidian.Obsidian", "--accept-source-agreements", "--accept-package-agreements", "--silent"]
    elif ctx.mac and ctx.which("brew"):
        cmd = ["brew", "install", "--cask", "obsidian"]
    elif not ctx.windows and not ctx.mac and ctx.which("flatpak"):
        ctx.runner(["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", "https://dl.flathub.org/repo/flathub.flatpakrepo"])
        cmd = ["flatpak", "install", "--user", "-y", "flathub", "md.obsidian.Obsidian"]
    else:
        where = {True: "winget is missing; get Obsidian from obsidian.md", False: "no installer found (Homebrew or Flatpak); get Obsidian from obsidian.md"}[ctx.windows]
        return result("obsidian", "manual", where + ", then run: python scripts/obsidian_vault.py --vault vault --register")
    code, out = ctx.runner(cmd)
    if code == 0 or obsidian_installed(ctx) or any(t in out.lower() for t in WINGET_OK_TEXT):
        return result("obsidian", "done", "installed with " + cmd[0] if code == 0 else "Obsidian was already there (%s said: %s)" % (cmd[0], out.splitlines()[-1][:80] if out else "code %s" % code))
    return result("obsidian", "failed", "%s failed (code %s): %s" % (cmd[0], code, out[-300:]))


def step_vault(ctx):
    """Register the vault folder in Obsidian's own config so it opens there. Needs Obsidian to be installed and closed."""
    if not obsidian_installed(ctx):
        return result("vault", "manual", "Obsidian is not installed, so the vault was not registered: install it from obsidian.md, then run: python scripts/obsidian_vault.py --vault vault --register")
    code, out = ctx.runner([ctx.python, ctx.script("obsidian_vault.py"), "--vault", ctx.vault, "--register"])
    if code == 0:
        return result("vault", "done", out.splitlines()[-1] if out else "registered")
    if code == 4:
        return result("vault", "manual", "Obsidian is running, so it was not changed (it would overwrite the change): close Obsidian, then run: python scripts/obsidian_vault.py --vault vault --register")
    return result("vault", "failed", "could not register the vault with Obsidian (code %s): %s" % (code, out[-300:]))


def step_scheduler(ctx):
    code, out = ctx.runner([ctx.python, ctx.script("install_scheduler.py"), "--apply"], 300)
    if code == 0:
        return result("scheduler", "done", out.splitlines()[-1] if out else "registered")
    if code == 3:
        return result("scheduler", "manual", (out.splitlines()[-1] if out else "no scheduler found") + "")
    return result("scheduler", "failed", "the scheduler is not working yet (%s); the loop still works when you run: python scripts/job_runner.py --once" % out[-300:])


def claude_config_path(ctx):
    folder = ctx.env.get("CLAUDE_CONFIG_DIR")
    return os.path.join(folder, ".claude.json") if folder else os.path.join(ctx.home, ".claude.json")


def trust_keys(folder):
    """The names Claude Code might use for a project folder: its real path and the path as written, with forward slashes."""
    keys = []
    for p in (os.path.realpath(folder), os.path.abspath(folder)):
        k = p.replace("\\", "/")
        if k not in keys:
            keys.append(k)
    return keys


def step_trust(ctx):
    path = claude_config_path(ctx)
    manual = "; the first start will ask once whether to trust the folder: answer yes"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("projects", {}), dict):
            raise ValueError("unexpected layout")
    except FileNotFoundError:
        return result("trust", "manual", "Claude Code has not been started on this computer yet" + manual)
    except (OSError, ValueError):
        return result("trust", "manual", "could not read Claude Code's settings file safely" + manual)
    projects = data.setdefault("projects", {})
    keys = trust_keys(ctx.out)
    if all(isinstance(projects.get(k), dict) and projects[k].get("hasTrustDialogAccepted") is True for k in keys):
        return result("trust", "skipped", "already trusted")
    for k in keys:
        projects.setdefault(k, {})["hasTrustDialogAccepted"] = True
    try:
        backup = path + ".bak_starter_kit"
        if os.path.exists(backup):                                          # the first backup is the untouched original: never overwrite it
            backup += "." + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, backup)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        shutil.copymode(path, tmp)                                          # the file holds sign-in data: keep its permissions
        os.replace(tmp, path)
        with open(path, encoding="utf-8") as f:                             # read it back: Claude Code may have been writing the same file
            again = json.load(f)
        if not all(again.get("projects", {}).get(k, {}).get("hasTrustDialogAccepted") is True for k in keys):
            return result("trust", "manual", "Claude Code rewrote its settings file while it was being changed" + manual)
    except OSError as e:
        return result("trust", "manual", "could not update Claude Code's settings file (%s)" % e + manual)
    return result("trust", "done", "folder marked as trusted (backup: %s)" % os.path.basename(backup))


def step_parity(ctx):
    code, out = ctx.runner([ctx.python, ctx.script("parity_test.py")], 1800)
    last = out.splitlines()[-1] if out else ""
    return result("parity", "done" if code == 0 else "failed", last if code == 0 else out[-900:])


def terminal_command(ctx):
    """-> the argv list that opens a new terminal window in the assistant folder running claude with a first prompt, or None when there is no way to do it here."""
    prompt = FIRST_PROMPT
    if ctx.windows:
        return [SHELL_EXECUTE, "cmd.exe", '/k claude "%s"' % prompt, ctx.out]            # opened through the shell, which gives the window a console of its own (see Context._launch)
    inner = "cd %s && claude %s" % (shlex.quote(ctx.out), shlex.quote(prompt))
    if ctx.mac:
        script = 'tell application "Terminal"\nactivate\ndo script "%s"\nend tell' % inner.replace("\\", "\\\\").replace('"', '\\"')
        return ["osascript", "-e", script]
    for term in ("gnome-terminal", "konsole", "xterm", "x-terminal-emulator"):
        if ctx.which(term):
            shell = inner + "; exec $SHELL"
            return [term, "--", "bash", "-c", shell] if term == "gnome-terminal" else [term, "-e", "bash", "-c", shell]
    return None


def step_launch(ctx):
    notes, problems = [], []
    again = step_trust(ctx)                                                 # Claude Code may have rewritten its settings since the trust step: make sure the first start will not ask
    if again["status"] == "done":
        notes.append("trust re-applied")
    if obsidian_installed(ctx):
        try:
            ctx.opener("obsidian://open?path=" + urllib.parse.quote(ctx.vault.replace("\\", "/"), safe="/:"))
            notes.append("vault opened in Obsidian")
        except Exception as e:                                              # never let a failed launch hide the finished setup
            problems.append("could not open Obsidian (%s): open the folder 'vault' in Obsidian yourself" % e)
    else:
        problems.append("Obsidian is not installed, so the vault was not opened (open the folder 'vault' in any Markdown editor)")
    cmd = terminal_command(ctx)
    if cmd is None:
        problems.append("no terminal program was found: open a terminal in %s and run: claude" % ctx.out)
    else:
        ok, detail = ctx.launcher(cmd)
        if ok:
            notes.append("assistant started in a new terminal")
        else:
            problems.append("could not open a terminal here (%s): open one in %s and run: claude" % (detail, ctx.out))
    return result("launch", "manual" if problems else "done", "; ".join(notes + problems))


STEPS = [("pytest", step_pytest), ("headless", step_headless), ("obsidian", step_obsidian), ("vault", step_vault), ("scheduler", step_scheduler),
         ("parity", step_parity), ("trust", step_trust), ("launch", step_launch)]
DESCRIPTIONS = {
    "pytest": "install pytest if it is missing",
    "headless": "(Windows) make background Python jobs run without console windows (one small file in your Python user folder; undo: python scripts/headless_subprocess.py --uninstall)",
    "obsidian": "install Obsidian if it is missing", "vault": "register the vault folder with Obsidian so it opens there",
    "scheduler": "register the job runner to run every 5 minutes and check that it ran", "parity": "run the seven end-to-end checks",
    "trust": "mark the new folder as trusted in Claude Code so it does not ask on first start", "launch": "open the vault in Obsidian and start the assistant in a new terminal",
}
DONE = ("done", "skipped")


def plan(skip=(), only=()):
    return [name for name, _fn in STEPS if (not only or name in only) and name not in skip]


def read_state(out):
    try:
        with open(os.path.join(out, "logs", "finish_setup.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(out, state):
    try:
        os.makedirs(os.path.join(out, "logs"), exist_ok=True)
        with open(os.path.join(out, "logs", "finish_setup.json"), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def run_steps(ctx, names, state=None, force=False, report=None):
    """-> list of result dicts in order. A step that raises is reported as failed and the rest still run. A step that finished on an earlier run (state[name] is done or skipped)
    is not repeated unless force; `report` is called with each result as soon as it exists."""
    fns = dict(STEPS)
    out = []
    state = {} if state is None else state
    for name in names:
        earlier = state.get(name, {}).get("status")
        if not force and earlier in DONE and name not in ("parity", "launch"):
            r = result(name, "skipped", "finished on an earlier run (use --force to repeat it)")      # the record keeps what really happened the first time
        else:
            try:
                r = fns[name](ctx)
            except Exception as e:
                r = result(name, "failed", "%s: %s" % (type(e).__name__, e))
            state[name] = {"status": r["status"], "detail": r["detail"][:300], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        out.append(r)
        if report:
            report(r)
    return out


def format_result(r):
    detail = r["detail"].strip()
    first, *rest = detail.splitlines() or [""]
    return "\n".join(["%-10s %-8s %s" % (r["name"], r["status"].upper(), first)] + ["%-19s%s" % ("", ln) for ln in rest])


def verdict(results):
    failed = [r["name"] for r in results if r["status"] == "failed"]
    manual = [r["name"] for r in results if r["status"] == "manual"]
    if not failed and not manual:
        return "RESULT: everything is set up"
    return "RESULT: %s%s" % (("failed: " + ", ".join(failed)) if failed else "", ((" " if failed else "") + "needs a manual step: " + ", ".join(manual)) if manual else "")


def format_results(results):
    return "\n".join([format_result(r) for r in results] + [verdict(results)])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Finish the assistant setup with no manual steps.")
    ap.add_argument("--out", help="the assistant folder that generate.py built")
    ap.add_argument("--yes", action="store_true", help="do it (without this only the plan is printed)")
    ap.add_argument("--skip", default="", help="comma-separated step names to skip")
    ap.add_argument("--only", default="", help="comma-separated step names to run")
    ap.add_argument("--force", action="store_true", help="run steps again that already finished")
    ap.add_argument("--status", action="store_true", help="show what the last run did")
    ap.add_argument("--trust-only", metavar="FOLDER", help="only mark FOLDER as trusted in Claude Code, then exit 0")
    args = ap.parse_args(argv)
    if args.trust_only:
        r = step_trust(Context(args.trust_only))
        print(format_result(r))
        return 0
    if not args.out:
        print("--out is required")
        return 2
    skip = [s for s in args.skip.split(",") if s]
    only = [s for s in args.only.split(",") if s]
    unknown = [s for s in skip + only if s not in dict(STEPS)]
    if unknown:
        print("unknown step(s): " + ", ".join(unknown))
        return 2
    if not os.path.isdir(os.path.join(args.out, "scripts")):
        print("%s does not look like a built assistant folder (no scripts folder)" % args.out)
        return 2
    if args.status:
        for name, v in read_state(args.out).items():
            print("%-10s %-8s %s  %s" % (name, v.get("status", "?").upper(), v.get("at", ""), v.get("detail", "")[:100]))
        return 0
    names = plan(skip, only)
    if not args.yes:
        print("plan (nothing is run without --yes):")
        for n in names:
            print("  %-10s %s" % (n, DESCRIPTIONS[n]))
        return 0
    lines = []

    def show(r):
        text = format_result(r)
        lines.append(text)
        print(text, flush=True)

    state = read_state(args.out)
    results = run_steps(Context(args.out), names, state, args.force, show)
    end = verdict(results)
    print(end, flush=True)
    write_state(args.out, state)
    try:
        with open(os.path.join(args.out, "logs", "finish_setup.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines + [end]) + "\n")
    except OSError:
        pass
    return 1 if any(r["status"] == "failed" for r in results) else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
