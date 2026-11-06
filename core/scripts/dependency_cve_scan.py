# -*- coding: utf-8 -*-
"""Dependency CVE scan: runs pip-audit (the Python Packaging Authority's vulnerability scanner) over every Python environment the assistant uses and alerts on known CVEs.

Environments scanned: the Python that runs this script, every virtual environment found inside the assistant's folder (a folder holding pyvenv.cfg, three levels deep
at most), and the interpreters listed in logs/dependency_cve_scan_envs.txt (one path per line, '#' starts a comment; the file name must start with "python").

    python scripts/dependency_cve_scan.py [--dry-run]

Setup: pip install pip-audit, in EACH environment you want checked (the scan runs `<that python> -m pip_audit`); an environment without it is reported as skipped with that
hint, never as clean. It needs network access (pip-audit asks the PyPI vulnerability service). --dry-run only lists what would be scanned and runs nothing.
pip-audit exits 1 when it finds vulnerabilities; that is read as a result, not a failure. Log: logs/dependency_cve_scan.jsonl. Kill switch: logs/dependency_cve_scan_off.txt.
A timed job always exits 0; findings go through notify.alert (one work item per distinct set of vulnerabilities).
Pure core (unit tested): discover_environments, parse_audit, scan_env, check."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

NAME = "dependency_cve_scan"
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

OFF_NAME = "dependency_cve_scan_off.txt"
LOG_NAME = "dependency_cve_scan.jsonl"
ENVS_NAME = "dependency_cve_scan_envs.txt"
SCAN_TIMEOUT = 180
MAX_OUTPUT = 5_000_000
MAX_READ = 1_000_000
MAX_LOG_BYTES = 1_000_000
MAX_ENVS = 12
SKIP_DIRS = {".git", "node_modules", "__pycache__", "site-packages", ".obsidian"}
NOT_INSTALLED = "pip-audit is not installed in this environment (pip install pip-audit)"


def logs_dir():
    return Path(LOGS)


def root_dir():
    return Path(ROOT)


def _alert(text, **kw):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        return notify.alert(text, source=NAME, logs=str(logs_dir()), **kw)
    except Exception:
        return None


def _venv_python(folder):
    for rel in (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3")):
        cand = Path(folder, *rel)
        if cand.is_file():
            return cand
    return None


def discover_environments(root=None, logs=None, current=None):
    """-> {name: interpreter path}: this Python, virtual environments under the assistant folder, and the interpreters named in the config file (at most MAX_ENVS)."""
    root, logs = Path(root or root_dir()), Path(logs or logs_dir())
    envs, seen = {}, set()

    def add(name, py):
        key = os.path.normcase(os.path.abspath(str(py)))
        if key not in seen and len(envs) < MAX_ENVS:
            seen.add(key)
            envs[name] = str(py)

    add("this-python", current or sys.executable)
    base_depth = len(root.parts)
    for cur, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and len(Path(cur, d).parts) - base_depth <= 3]
        if "pyvenv.cfg" in files:
            py = _venv_python(cur)
            if py:
                add(Path(cur).relative_to(root).as_posix() or "venv", py)
            dirs[:] = []
    try:
        text = (logs / ENVS_NAME).read_text(encoding="utf-8", errors="replace")[:MAX_READ]
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and Path(line).name.lower().startswith("python"):
            add("config:" + Path(line).parent.parent.name + "/" + Path(line).name, line)
    return envs


def parse_audit(text):
    """Pure. pip-audit's JSON -> [{package, version, id, fix_versions}]. Raises ValueError when the text is not pip-audit JSON."""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("dependencies"), list):
        raise ValueError("not a pip-audit report")
    out = []
    for dep in data["dependencies"]:
        if not isinstance(dep, dict):
            continue
        for v in dep.get("vulns") or []:
            if isinstance(v, dict) and v.get("id"):
                fixes = v.get("fix_versions")
                out.append({"package": str(dep.get("name", "?"))[:80], "version": str(dep.get("version", "?"))[:40], "id": str(v["id"])[:60],
                            "fix_versions": [str(x)[:40] for x in fixes][:5] if isinstance(fixes, list) else []})
    return out


def run_command(argv, timeout):
    """Run a fixed argv. Raises subprocess.TimeoutExpired / OSError; the caller turns those into a skipped result."""
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                          creationflags=0x08000000 if os.name == "nt" else 0)


def scan_env(python_path, run=None):
    """-> {'vulnerabilities': [...]} or {'error': why}. Never raises."""
    if not Path(python_path).is_file():
        return {"error": "interpreter not found"}
    try:
        r = (run or run_command)([str(python_path), "-m", "pip_audit", "--format", "json", "--progress-spinner", "off"], SCAN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "timed out"}
    except OSError as e:
        return {"error": "could not start: %s" % type(e).__name__}
    out = (r.stdout or "")[:MAX_OUTPUT].strip()
    if not out:
        err = r.stderr or ""
        if "pip_audit" in err and "No module named" in err:
            return {"error": NOT_INSTALLED, "tool_missing": True}
        return {"error": (err.strip() or "no output")[:300]}
    try:
        return {"vulnerabilities": parse_audit(out)}
    except ValueError:
        return {"error": "pip-audit output was not understood: " + (r.stderr or out)[:200].replace("\n", " ")}


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


def check(dry_run=False, run=None, envs=None, now=None):
    now = time.time() if now is None else now
    logs = logs_dir()
    if (logs / OFF_NAME).exists():
        return {"off": True}
    envs = envs if envs is not None else discover_environments()
    if dry_run:
        return {"dry_run": True, "environments": envs, "total_vulnerabilities": 0}
    results = {name: scan_env(py, run) for name, py in envs.items()}
    total = sum(len(r.get("vulnerabilities", [])) for r in results.values())
    result = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), "environments": results, "total_vulnerabilities": total, "dry_run": False}
    append_log(logs / LOG_NAME, result)
    if total:
        ids = sorted({"%s %s" % (v["package"], v["id"]) for r in results.values() for v in r.get("vulnerabilities", [])})
        first = next(v for r in results.values() for v in r.get("vulnerabilities", []))
        _alert("%d known vulnerabilit%s in %d Python environment(s), e.g. %s==%s %s (fix: %s)"
               % (total, "y" if total == 1 else "ies", sum(1 for r in results.values() if r.get("vulnerabilities")), first["package"], first["version"], first["id"],
                  ", ".join(first["fix_versions"]) or "none known"),
               intent_kind="dependency_cve", tier=2, value=3, dedupe="depcve-" + hashlib.sha1("|".join(ids).encode("utf-8")).hexdigest()[:10])
    return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog=NAME, description="run pip-audit over the assistant's Python environments")
    ap.add_argument("--dry-run", action="store_true", help="list the environments that would be scanned; run nothing")
    a = ap.parse_args(argv)
    try:
        r = check(dry_run=a.dry_run)
    except Exception as e:
        print("[ERROR] %s: %s" % (type(e).__name__, str(e)[:120]))
        return 0
    if r.get("off"):
        print("[OFF] logs/%s exists" % OFF_NAME)
        return 0
    if r.get("dry_run"):
        for name, py in r["environments"].items():
            print("would scan %s: %s" % (name, py))
        return 0
    for name, data in r["environments"].items():
        if "error" in data:
            print("%s: [SKIPPED] %s" % (name, data["error"]))
        else:
            print("%s: %d vulnerability(ies)" % (name, len(data["vulnerabilities"])))
            for v in data["vulnerabilities"]:
                print("    %s==%s  %s  fix: %s" % (v["package"], v["version"], v["id"], v["fix_versions"]))
    print("Total: %d" % r["total_vulnerabilities"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
