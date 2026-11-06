# -*- coding: utf-8 -*-
"""Registers the job runner with the operating system so the work loop runs every 5 minutes without you opening anything, then checks that it really ran.

    python scripts/install_scheduler.py              # show what would be done (changes nothing)
    python scripts/install_scheduler.py --apply      # register it, start it once and wait for its heartbeat (exit 0 verified, 1 not verified or failed, 3 no scheduler here)
    python scripts/install_scheduler.py --apply --no-verify
    python scripts/install_scheduler.py --status     # is it registered (and how)
    python scripts/install_scheduler.py --remove     # unregister it again and delete what --apply wrote

Windows   one Task Scheduler task named "Assistant JobRunner - <folder name>" (env HARNESS_TASK_NAME overrides), registered from an XML definition so that it also runs on battery,
          is hidden, and runs once more at the next start when a run was missed; it uses pythonw.exe when that exists, so no console window appears.
macOS     a launchd user agent, com.assistant.jobrunner.<slug>.plist in ~/Library/LaunchAgents.
Linux     a systemd user timer (assistant-jobrunner-<slug>.timer + .service in ~/.config/systemd/user); when systemd is not available a crontab line (the old crontab is saved first to
          logs/crontab.backup.<time>); when there is neither, it prints what to do by hand and exits 3.
<slug> is the folder name in lower case with anything that is not a letter or digit turned into a dash. Nothing else on the computer is touched.
Pure core (unit tested): pick_python, task_command, task_xml, task_name, slug, plist_dict, systemd_units, cron_line, add_cron, remove_cron, heartbeat_time, verify."""
import argparse
import datetime
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

MARKER = "# assistant-job-runner (installed by scripts/install_scheduler.py)"
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
VERIFY_SECONDS = 60


def default_root():
    return os.environ.get("HARNESS_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def logs_dir(root):
    return os.environ.get("HARNESS_LOGS") or os.path.join(root, "logs")


class Result:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def system_runner(args, input_text=None):
    """Run a program. Never raises: a missing program is exit 127, a hang is exit 124. No console window on Windows."""
    try:
        r = subprocess.run(args, input=input_text, capture_output=True, encoding="utf-8", errors="replace", timeout=120, creationflags=0x08000000 if os.name == "nt" else 0,
                           stdin=subprocess.DEVNULL if input_text is None else None)
        return Result(r.returncode, r.stdout or "", r.stderr or "")
    except subprocess.TimeoutExpired:
        return Result(124, "", "timed out")
    except OSError as e:
        return Result(127, "", str(e))


# ---------- names and commands ----------
def pick_python(python=None):
    """pythonw.exe (no console window) when it sits next to the python that runs this; otherwise that python itself."""
    python = python or sys.executable
    folder, name = os.path.split(python)
    if name.lower() == "python.exe":
        windowless = os.path.join(folder, "pythonw.exe")
        if os.path.exists(windowless):
            return windowless
    return python


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "assistant"


def task_name(root, env=None):
    """'Assistant JobRunner - <folder name>' (env HARNESS_TASK_NAME overrides); characters Task Scheduler does not allow in a name are replaced by a space."""
    env = os.environ if env is None else env
    name = env.get("HARNESS_TASK_NAME") or "Assistant JobRunner - %s" % (os.path.basename(os.path.normpath(root)) or "assistant")
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", name)).strip()


def task_command(root, python):
    return '"%s" "%s" --once' % (python, os.path.join(root, "scripts", "job_runner.py"))


def job_runner_path(root):
    return os.path.join(root, "scripts", "job_runner.py")


# ---------- Windows ----------
def task_xml(root, python, start=None):
    """The Task Scheduler definition, UTF-16 with a byte-order mark as schtasks wants it: every 5 minutes from `start` with no end, on battery too, hidden, one copy at a time,
    run at the next chance when a run was missed, ten minutes at most per run, as the current user without elevation."""
    start = start or datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    text = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="%s">\n'
        '  <RegistrationInfo><Description>%s</Description></RegistrationInfo>\n'
        '  <Triggers><TimeTrigger><Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>'
        '<StartBoundary>%s</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>\n'
        '  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n'
        '  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>'
        '<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable>'
        '<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>true</Hidden>'
        '<ExecutionTimeLimit>PT10M</ExecutionTimeLimit><Priority>7</Priority></Settings>\n'
        '  <Actions Context="Author"><Exec><Command>%s</Command><Arguments>%s</Arguments><WorkingDirectory>%s</WorkingDirectory></Exec></Actions>\n'
        '</Task>\n'
    ) % (NS, xml_escape("Runs scripts/job_runner.py --once every 5 minutes for the assistant in " + root), xml_escape(start), xml_escape(python),
         xml_escape('"%s" --once' % job_runner_path(root)), xml_escape(root))
    return text.encode("utf-16")


def windows_delete_args(name):
    return ["schtasks", "/Delete", "/TN", name, "/F"]


# ---------- macOS ----------
def plist_dict(root, python, slug_, logs):
    return {"Label": "com.assistant.jobrunner." + slug_, "ProgramArguments": [python, job_runner_path(root), "--once"], "WorkingDirectory": root, "StartInterval": 300, "RunAtLoad": True,
            "StandardOutPath": os.path.join(logs, "job_runner.out"), "StandardErrorPath": os.path.join(logs, "job_runner.out")}


# ---------- Linux ----------
def systemd_quote(arg):
    """One ExecStart argument: in double quotes, with backslash, quote, percent and dollar signs escaped the way systemd reads them."""
    return '"%s"' % arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")


def systemd_units(root, python, name):
    service = ("[Unit]\nDescription=Assistant job runner for %s\n\n[Service]\nType=oneshot\nWorkingDirectory=%s\nExecStart=%s %s --once\n"
               % (name.replace("%", "%%"), root.replace("%", "%%"), systemd_quote(python), systemd_quote(job_runner_path(root))))
    timer = "[Unit]\nDescription=Run the assistant job runner every 5 minutes\n\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec=5min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    return service, timer


def cron_line(root, python):
    """The crontab line. A single quote in a path is written '\\'' and a percent sign \\% (cron reads a bare % as a line break)."""
    def esc(s):
        return s.replace("'", "'\\''").replace("%", "\\%")
    return "*/5 * * * * cd '%s' && '%s' scripts/job_runner.py --once >> logs/job_runner.out 2>&1" % (esc(root), esc(python))


def add_cron(existing, line):
    """New crontab text with our marked line added once (an old marked line is replaced). Other lines are kept exactly."""
    kept = remove_cron(existing)
    base = kept if kept.endswith("\n") or not kept else kept + "\n"
    return base + MARKER + "\n" + line + "\n"


def remove_cron(existing):
    """Crontab text without our marker and the line that follows it."""
    out, skip = [], False
    for ln in (existing or "").splitlines():
        if ln.strip() == MARKER:
            skip = True
            continue
        if skip:
            skip = False
            continue
        out.append(ln)
    return "\n".join(out) + ("\n" if out else "")


# ---------- checking that it ran ----------
def heartbeat_time(logs):
    """The time the job runner last wrote logs/heartbeats/job_runner.beat, or None."""
    try:
        return float((Path(logs) / "heartbeats" / "job_runner.beat").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def verify(start_job, logs, sleep=time.sleep, clock=time.time, timeout=VERIFY_SECONDS):
    """Note the heartbeat, start the job once, wait up to `timeout` seconds for a newer heartbeat. -> (ok, message)."""
    before = heartbeat_time(logs)
    started = clock()
    start_job()
    while True:
        after = heartbeat_time(logs)
        if after is not None and (before is None or after > before):
            return True, "verified: the job runner ran at " + time.strftime("%H:%M:%S", time.localtime(after))
        if clock() - started >= timeout:
            return False, "registered, but the job runner did not run yet (waited %d seconds)" % timeout
        sleep(1.0)


# ---------- the three systems ----------
class Plan:
    """What one system needs: describe it, apply it, remove it, say whether it is registered, start the job once."""

    def __init__(self, mechanism, describe, apply, remove, status, start):
        self.mechanism, self.describe, self.apply, self.remove, self.status, self.start = mechanism, describe, apply, remove, status, start


def out_of(r):
    return ((r.stdout or "").strip() or (r.stderr or "").strip())


def windows_plan(root, python, runner, env):
    name = task_name(root, env)

    def apply():
        fd, path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(task_xml(root, python))
            r = runner(["schtasks", "/Create", "/XML", path, "/TN", name, "/F"])
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return r.returncode == 0, out_of(r)

    def remove():
        r = runner(windows_delete_args(name))
        return r.returncode == 0, out_of(r)

    def status():
        return runner(["schtasks", "/Query", "/TN", name]).returncode == 0

    return Plan("Windows Task Scheduler", ["task name: " + name, "command: " + task_command(root, python), "every 5 minutes, also on battery, hidden"], apply, remove, status,
                lambda: runner(["schtasks", "/Run", "/TN", name]))


def mac_plan(root, python, runner, home, logs):
    sl = slug(os.path.basename(os.path.normpath(root)))
    label = "com.assistant.jobrunner." + sl
    plist = Path(home) / "Library" / "LaunchAgents" / (label + ".plist")
    uid = str(os.getuid()) if hasattr(os, "getuid") else "501"

    def apply():
        plist.parent.mkdir(parents=True, exist_ok=True)
        with plist.open("wb") as f:
            plistlib.dump(plist_dict(root, python, sl, logs), f)
        r = runner(["launchctl", "bootstrap", "gui/" + uid, str(plist)])
        if r.returncode != 0:
            r = runner(["launchctl", "load", "-w", str(plist)])
        return r.returncode == 0, out_of(r)

    def remove():
        r = runner(["launchctl", "bootout", "gui/%s/%s" % (uid, label)])
        if r.returncode != 0:
            r = runner(["launchctl", "unload", "-w", str(plist)])
        existed = plist.exists()
        try:
            plist.unlink()
        except OSError:
            pass
        return r.returncode == 0 or not existed, out_of(r)

    def status():
        return plist.exists()

    return Plan("launchd user agent", ["agent: %s" % plist, "every 5 minutes and at login"], apply, remove, status,
                lambda: runner(["launchctl", "kickstart", "-k", "gui/%s/%s" % (uid, label)]))


def linux_systemd_plan(root, python, runner, home):
    sl = slug(os.path.basename(os.path.normpath(root)))
    base = "assistant-jobrunner-" + sl
    folder = Path(home) / ".config" / "systemd" / "user"
    service_path, timer_path = folder / (base + ".service"), folder / (base + ".timer")

    def apply():
        service, timer = systemd_units(root, python, os.path.basename(os.path.normpath(root)))
        folder.mkdir(parents=True, exist_ok=True)
        service_path.write_text(service, encoding="utf-8")
        timer_path.write_text(timer, encoding="utf-8")
        r = runner(["systemctl", "--user", "daemon-reload"])
        if r.returncode == 0:
            r = runner(["systemctl", "--user", "enable", "--now", base + ".timer"])
        return r.returncode == 0, out_of(r) or "timer enabled (it runs while you are logged in; run: loginctl enable-linger $USER to keep it running after you log out)"

    def remove():
        r = runner(["systemctl", "--user", "disable", "--now", base + ".timer"])
        existed = service_path.exists() or timer_path.exists()
        for p in (service_path, timer_path):
            try:
                p.unlink()
            except OSError:
                pass
        runner(["systemctl", "--user", "daemon-reload"])
        return r.returncode == 0 or not existed, out_of(r)

    return Plan("systemd user timer", ["service: %s" % service_path, "timer: %s" % timer_path, "every 5 minutes, 2 minutes after boot"], apply, remove,
                lambda: timer_path.exists() and runner(["systemctl", "--user", "is-enabled", base + ".timer"]).returncode == 0,
                lambda: runner(["systemctl", "--user", "start", base + ".service"]))


def cron_plan(root, python, runner, logs):
    line = cron_line(root, python)

    def current():
        r = runner(["crontab", "-l"])
        return r.stdout if r.returncode == 0 else ""

    def apply():
        existing = current()
        if existing.strip():
            try:
                Path(logs).mkdir(parents=True, exist_ok=True)
                (Path(logs) / ("crontab.backup." + time.strftime("%Y%m%d-%H%M%S"))).write_text(existing, encoding="utf-8")
            except OSError:
                pass
        r = runner(["crontab", "-"], add_cron(existing, line))
        return r.returncode == 0, out_of(r) or "crontab line added"

    def remove():
        r = runner(["crontab", "-"], remove_cron(current()))
        return r.returncode == 0, out_of(r) or "crontab line removed"

    return Plan("crontab", ["crontab line: " + line], apply, remove, lambda: MARKER in current(), lambda: runner([python, job_runner_path(root), "--once"]))


def choose_plan(root, python, runner, windows, mac, home, env, which):
    logs = logs_dir(root)
    if windows:
        return windows_plan(root, python, runner, env)
    if mac:
        return mac_plan(root, python, runner, home, logs)
    if runner(["systemctl", "--user", "show-environment"]).returncode == 0:
        return linux_systemd_plan(root, python, runner, home)
    probe = runner(["crontab", "-l"])
    if probe.returncode == 0 or "no crontab" in ((probe.stderr or "") + (probe.stdout or "")).lower():
        return cron_plan(root, python, runner, logs)
    return None


def main(argv=None, runner=None, windows=None, mac=None, home=None, root=None, sleep_fn=time.sleep, time_fn=time.time, env=None, python=None):
    ap = argparse.ArgumentParser(description="Register the job runner with the OS scheduler.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="register it and check that it ran")
    mode.add_argument("--remove", action="store_true", help="unregister it again")
    mode.add_argument("--status", action="store_true", help="show whether it is registered")
    ap.add_argument("--no-verify", action="store_true", help="with --apply: do not wait for the first run")
    args = ap.parse_args(argv)
    runner = runner or system_runner
    windows = (os.name == "nt") if windows is None else windows
    mac = (sys.platform == "darwin") if mac is None else mac
    home = home or str(Path.home())
    root = os.path.abspath(root or default_root())
    env = os.environ if env is None else env
    python = pick_python(python)
    plan = choose_plan(root, python, runner, windows, mac, home, env, None)
    if plan is None:
        print("manual: no scheduler found; run python scripts/job_runner.py --once every 5 minutes yourself")
        return 3
    if args.status:
        print("registered: " + plan.mechanism if plan.status() else "not registered (%s would be used)" % plan.mechanism)
        return 0
    if args.remove:
        ok, msg = plan.remove()
        print(("removed from %s" % plan.mechanism) + ((": " + msg) if msg else "") if ok else "could not remove it: " + msg)
        return 0 if ok else 1
    print("%s: %s" % ("register" if args.apply else "would register", plan.mechanism))
    for line in plan.describe:
        print("  " + line)
    if not args.apply:
        print("dry run: nothing changed. Add --apply to register it.")
        return 0
    ok, msg = plan.apply()
    if msg:
        print(msg)
    if not ok:
        print("could not register it")
        return 1
    if args.no_verify:
        return 0
    print("verifying...")
    good, text = verify(plan.start, logs_dir(root), sleep_fn, time_fn)
    print(text)
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
