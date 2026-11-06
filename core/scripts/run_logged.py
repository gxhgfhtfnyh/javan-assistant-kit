# -*- coding: utf-8 -*-
"""Runs a Python script with no console window and appends its output to a log file (use it instead of scheduled tasks of the form `cmd /c python.exe x.py >> x.log`, whose cmd window flashes on screen).
    pythonw.exe run_logged.py <logfile> <script.py> [args...]
The script runs under python.exe (so its output can be captured) with CREATE_NO_WINDOW; its output and a one-line header with the start time and exit code are appended to <logfile>.
Exit code: the script's own, 124 when it ran longer than --timeout (default 1800 s) and was killed, 2 for wrong usage. Pure core (unit tested): build_command, header, run."""
import os
import subprocess
import sys
import time

NO_WINDOW = 0x08000000
DEFAULT_TIMEOUT = 1800


def console_python(exe=None):
    """python.exe next to the running interpreter: pythonw.exe has no stdout to capture."""
    exe = exe or sys.executable
    folder, name = os.path.split(exe)
    if name.lower() == "pythonw.exe":
        candidate = os.path.join(folder, "python.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def build_command(script, args, exe=None):
    return [console_python(exe), script] + list(args)


def header(script, started, code):
    return "=== %s %s exit %s ===\n" % (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)), os.path.basename(script), code)


def run(logfile, script, args=(), timeout=DEFAULT_TIMEOUT, exe=None):
    """-> exit code. The log gets the header and the output even when the script fails or times out."""
    started = time.time()
    os.makedirs(os.path.dirname(os.path.abspath(logfile)), exist_ok=True)
    flags = NO_WINDOW if os.name == "nt" else 0
    try:
        r = subprocess.run(build_command(script, args, exe), capture_output=True, timeout=timeout, creationflags=flags, stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        out, code = (r.stdout or b"") + (r.stderr or b""), r.returncode
    except subprocess.TimeoutExpired as e:
        out, code = (e.stdout or b"") + b"\n[run_logged: killed after %d s]\n" % timeout, 124
    except OSError as e:
        out, code = ("[run_logged: could not start: %s]\n" % e).encode("utf-8"), 127
    with open(logfile, "ab") as f:
        f.write(header(script, started, code).encode("utf-8"))
        f.write(out if out.endswith(b"\n") or not out else out + b"\n")
    return code


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    timeout = DEFAULT_TIMEOUT
    if argv[:1] == ["--timeout"] and len(argv) > 1:
        try:
            timeout = int(argv[1])
        except ValueError:
            return 2
        argv = argv[2:]
    if len(argv) < 2:
        return 2
    return run(argv[0], argv[1], argv[2:], timeout)


if __name__ == "__main__":
    sys.exit(main())
