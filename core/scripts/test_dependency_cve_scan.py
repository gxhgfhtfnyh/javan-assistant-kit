# -*- coding: utf-8 -*-
"""Tests for dependency_cve_scan.py: finding the environments, reading pip-audit's report, the skip paths, alerts, dry-run, the kill switch and the real process.
No test starts pip-audit or touches the network: the command runner is always a fake."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dependency_cve_scan as m  # noqa: E402

REPORT = json.dumps({"dependencies": [
    {"name": "requests", "version": "2.0.0", "vulns": [{"id": "PYSEC-2023-1", "fix_versions": ["2.31.0"], "aliases": ["CVE-2023-1"]}]},
    {"name": "safe", "version": "1.0", "vulns": []},
    {"name": "local-only", "skip_reason": "Dependency not found on PyPI"},
], "fixes": []})
CLEAN = json.dumps({"dependencies": [{"name": "safe", "version": "1.0", "vulns": []}]})


class R:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m, "LOGS", root / "logs")
    return root


def fake_python(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def alerts(root):
    p = root / "logs" / "alerts.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def test_parse_audit_extracts_only_real_vulnerabilities():
    assert m.parse_audit(REPORT) == [{"package": "requests", "version": "2.0.0", "id": "PYSEC-2023-1", "fix_versions": ["2.31.0"]}]


@pytest.mark.parametrize("bad", ["", "not json", "[]", json.dumps({"dependencies": "x"}), json.dumps({"other": 1}), "null"])
def test_parse_audit_rejects_things_that_are_not_a_report(bad):
    with pytest.raises(ValueError):
        m.parse_audit(bad)


def test_parse_audit_tolerates_odd_entries_and_caps_long_fields():
    odd = json.dumps({"dependencies": [5, None, {"name": "x" * 500, "version": "1", "vulns": [{"id": "A"}, {"no_id": 1}, "str", {"id": "B", "fix_versions": "oops"}]}]})
    got = m.parse_audit(odd)
    assert [v["id"] for v in got] == ["A", "B"] and len(got[0]["package"]) == 80 and got[0]["fix_versions"] == [] and got[1]["fix_versions"] == []


def test_scan_env_reports_vulnerabilities_even_when_pip_audit_exits_one(tmp_path):
    py = fake_python(tmp_path / "py" / "python")
    seen = {}

    def run(argv, timeout):
        seen["argv"], seen["timeout"] = argv, timeout
        return R(REPORT, "", 1)
    out = m.scan_env(str(py), run)
    assert out["vulnerabilities"][0]["id"] == "PYSEC-2023-1"
    assert seen["argv"][:3] == [str(py), "-m", "pip_audit"] and "--format" in seen["argv"] and seen["timeout"] == m.SCAN_TIMEOUT


def test_scan_env_clean_environment(tmp_path):
    assert m.scan_env(str(fake_python(tmp_path / "python")), lambda a, t: R(CLEAN)) == {"vulnerabilities": []}


def test_scan_env_missing_interpreter(tmp_path):
    assert m.scan_env(str(tmp_path / "nope"), lambda a, t: R(CLEAN)) == {"error": "interpreter not found"}


def test_scan_env_says_how_to_install_pip_audit_when_missing(tmp_path):
    out = m.scan_env(str(fake_python(tmp_path / "python")), lambda a, t: R("", "C:\\x\\python.exe: No module named pip_audit", 1))
    assert out["tool_missing"] is True and "pip install pip-audit" in out["error"]


def test_scan_env_timeout_oserror_and_garbage(tmp_path):
    py = str(fake_python(tmp_path / "python"))

    def slow(a, t):
        raise subprocess.TimeoutExpired(a, t)

    def nope(a, t):
        raise PermissionError("denied")
    assert m.scan_env(py, slow) == {"error": "timed out"}
    assert "could not start" in m.scan_env(py, nope)["error"]
    assert "not understood" in m.scan_env(py, lambda a, t: R("<html>", ""))["error"]
    assert m.scan_env(py, lambda a, t: R("", "network unreachable"))["error"] == "network unreachable"
    assert m.scan_env(py, lambda a, t: R("", ""))["error"] == "no output"


def test_discover_environments_has_this_python_venvs_and_config_entries(sandbox, tmp_path):
    cur = fake_python(tmp_path / "sys" / "python")
    venv_py = fake_python(sandbox / "tools" / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    (sandbox / "tools" / ".venv" / "pyvenv.cfg").write_text("home = x", encoding="utf-8")
    other = fake_python(tmp_path / "other" / "Python311" / "python3.11")
    (sandbox / "logs" / m.ENVS_NAME).write_text("# mine\n%s\n/usr/bin/not-python-at-all\n\n" % other, encoding="utf-8")
    envs = m.discover_environments(current=str(cur))
    assert envs["this-python"] == str(cur)
    assert str(venv_py) in envs.values()
    assert str(other) in envs.values() and not any("not-python" in v for v in envs.values())


def test_discover_environments_does_not_list_the_same_interpreter_twice(sandbox):
    venv_py = fake_python(sandbox / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    (sandbox / ".venv" / "pyvenv.cfg").write_text("x", encoding="utf-8")
    envs = m.discover_environments(current=str(venv_py))
    assert list(envs.values()) == [str(venv_py)]


def test_discover_environments_ignores_deep_folders_and_caps_the_count(sandbox, monkeypatch):
    deep = sandbox / "a" / "b" / "c" / "d" / ".venv"
    fake_python(deep / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    (deep / "pyvenv.cfg").write_text("x", encoding="utf-8")
    assert len(m.discover_environments(current="/x/python")) == 1
    monkeypatch.setattr(m, "MAX_ENVS", 2)
    lines = [str(fake_python(sandbox.parent / ("e%d" % i) / "python")) for i in range(5)]
    (sandbox / "logs" / m.ENVS_NAME).write_text("\n".join(lines), encoding="utf-8")
    assert len(m.discover_environments(current="/x/python")) == 2


def test_check_with_vulnerabilities_logs_alerts_once_and_raises_one_work_item(sandbox, tmp_path):
    envs = {"a": str(fake_python(tmp_path / "a" / "python")), "b": str(fake_python(tmp_path / "b" / "python"))}
    r = m.check(run=lambda argv, t: R(REPORT, "", 1), envs=envs)
    assert r["total_vulnerabilities"] == 2
    assert len(alerts(sandbox)) == 1 and "requests==2.0.0 PYSEC-2023-1" in alerts(sandbox)[0] and "2.31.0" in alerts(sandbox)[0]
    items = [json.loads(x) for x in (sandbox / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(items) == 1 and items[0]["kind"] == "dependency_cve" and items[0]["tier"] == 2
    m.check(run=lambda argv, t: R(REPORT, "", 1), envs=envs)
    assert len((sandbox / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert len((sandbox / "logs" / m.LOG_NAME).read_text(encoding="utf-8").splitlines()) == 2


def test_check_clean_and_skipped_environments_raise_no_alert(sandbox, tmp_path):
    envs = {"ok": str(fake_python(tmp_path / "ok" / "python")), "gone": str(tmp_path / "gone" / "python")}
    r = m.check(run=lambda argv, t: R(CLEAN), envs=envs)
    assert r["total_vulnerabilities"] == 0 and "error" in r["environments"]["gone"] and alerts(sandbox) == []


def test_dry_run_runs_nothing_and_writes_nothing(sandbox, tmp_path):
    def boom(argv, t):
        raise AssertionError("must not run")
    r = m.check(dry_run=True, run=boom, envs={"a": str(fake_python(tmp_path / "python"))})
    assert r["dry_run"] and "a" in r["environments"] and not (sandbox / "logs" / m.LOG_NAME).exists()


def test_kill_switch(sandbox, tmp_path):
    (sandbox / "logs" / m.OFF_NAME).write_text("x", encoding="utf-8")
    assert m.check(run=lambda a, t: R(REPORT), envs={"a": str(fake_python(tmp_path / "python"))}) == {"off": True}


def test_main_prints_a_summary_and_always_exits_zero(sandbox, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(m, "discover_environments", lambda: {"a": str(fake_python(tmp_path / "python"))})
    monkeypatch.setattr(m, "run_command", lambda argv, t: R(REPORT, "", 1))
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "a: 1 vulnerability(ies)" in out and "requests==2.0.0" in out and "Total: 1" in out
    assert m.main(["--dry-run"]) == 0 and "would scan a:" in capsys.readouterr().out
    monkeypatch.setattr(m, "check", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert m.main([]) == 0 and "[ERROR]" in capsys.readouterr().out


def test_real_process_dry_run_runs_nothing_and_exits_zero(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(logs), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "dependency_cve_scan.py"), "--dry-run"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "would scan this-python" in r.stdout and not (logs / m.LOG_NAME).exists()
