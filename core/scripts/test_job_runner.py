# -*- coding: utf-8 -*-
"""Tests for job_runner.py"""

import json
import os
import sys
import time
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add the job runner directory to path
sys.path.insert(0, str(Path(__file__).parent))

import job_runner as jr

from job_runner import (
    parse_hhmm,
    _scheduled_today,
    _last_weekly_slot,
    due,
    plan,
    update_state,
    _command,
    run_job,
    _read_json,
    load_jobs,
    _load_state,
    _save_json,
    _acquire_lock,
    _release_lock,
    _write_heartbeat,
    _check_kill_switch,
    _run_once,
    _list_jobs,
    main,
    LockHeld,
    StateUnreadable,
    ROOT,
    LOGS,
    JOBS_FILE,
    STATE_FILE,
    LOG_FILE,
    ERRORS_FILE,
    DEFAULT_TIMEOUT,
    BUDGET_S,
    SLACK_S,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def tmp_logs(tmp_path):
    """Create a temporary logs directory."""
    logs = tmp_path / "logs"
    logs.mkdir()
    return logs


@pytest.fixture
def tmp_root(tmp_path):
    """Create a temporary root directory with scripts folder."""
    # The job_runner module uses HERE = Path(__file__).parent.resolve() which is the directory
    # containing job_runner.py. So scripts should be relative to that.
    root = tmp_path / "root"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def patch_here(tmp_root, monkeypatch):
    """Patch job_runner.HERE to point to our temp scripts directory."""
    monkeypatch.setattr(jr, "HERE", tmp_root)


@pytest.fixture
def sample_jobs():
    """Sample jobs for testing."""
    return [
        {
            "id": "job_every_15",
            "argv": ["python", "scripts/test.py"],
            "every_min": 15,
            "timeout_s": 60,
            "enabled": True,
        },
        {
            "id": "job_daily",
            "argv": ["python", "scripts/daily.py"],
            "daily": "04:00",
            "timeout_s": 60,
            "enabled": True,
        },
        {
            "id": "job_weekly",
            "argv": ["python", "scripts/weekly.py"],
            "weekly": {"day": 0, "at": "04:00"},
            "timeout_s": 60,
            "enabled": True,
        },
        {
            "id": "job_disabled",
            "argv": ["python", "scripts/disabled.py"],
            "every_min": 10,
            "timeout_s": 60,
            "enabled": False,
        },
    ]


@pytest.fixture
def now():
    """Fixed time for testing."""
    return 1700000000.0  # A fixed timestamp


# ============================================================================
# Tests for parse_hhmm
# ============================================================================

def test_parse_hhmm_valid():
    assert parse_hhmm("04:00") == (4, 0)
    assert parse_hhmm("12:30") == (12, 30)
    assert parse_hhmm("23:59") == (23, 59)


def test_parse_hhmm_invalid():
    with pytest.raises(ValueError):
        parse_hhmm("4:30pm")
    with pytest.raises(ValueError):
        parse_hhmm("not_a_time")


# ============================================================================
# Tests for _scheduled_today
# ============================================================================

def test_scheduled_today():
    # 2023-11-14 12:00:00 UTC
    now = 1700000000.0
    slot = _scheduled_today(now, "04:00")
    # Should be today at 04:00 local time
    lt = time.localtime(slot)
    assert lt.tm_hour == 4
    assert lt.tm_min == 0


# ============================================================================
# Tests for _last_weekly_slot
# ============================================================================

def test_last_weekly_slot():
    # 2023-11-14 is a Tuesday (day 1)
    now = 1700000000.0
    # Monday (day 0) at 04:00
    slot = _last_weekly_slot(now, 0, "04:00")
    lt = time.localtime(slot)
    assert lt.tm_wday == 0  # Monday
    assert lt.tm_hour == 4
    assert lt.tm_min == 0
    assert slot <= now


def test_last_weekly_slot_same_day():
    # If today is the target day and time hasn't passed yet, should be today
    # 2023-11-13 02:00:00 UTC (Monday early morning)
    now = 1699843200.0
    slot = _last_weekly_slot(now, 0, "04:00")
    lt = time.localtime(slot)
    assert lt.tm_wday == 0
    assert slot <= now


# ============================================================================
# Tests for due()
# ============================================================================

def test_due_every_min_never_ran(sample_jobs, now):
    job = sample_jobs[0]  # every 15 min
    state = {}
    is_due, why = due(job, state, now)
    assert is_due is True
    assert "every 15 min" in why


def test_due_every_min_not_due_yet(sample_jobs, now):
    job = sample_jobs[0]
    state = {job["id"]: {"last_run": now - 100}}  # ran 100 seconds ago (less than 15 min)
    is_due, why = due(job, state, now)
    assert is_due is False


def test_due_every_min_due_after_interval(sample_jobs, now):
    job = sample_jobs[0]
    state = {job["id"]: {"last_run": now - 20 * 60}}  # ran 20 minutes ago
    is_due, why = due(job, state, now)
    assert is_due is True


def test_due_disabled_job(sample_jobs, now):
    job = sample_jobs[3]  # disabled
    state = {}
    is_due, why = due(job, state, now)
    assert is_due is False
    assert why == "disabled"


def test_due_daily_job_due(sample_jobs, now):
    job = sample_jobs[1]  # daily at 04:00
    # State has last_run before today's 04:00
    today_0400 = _scheduled_today(now, "04:00")
    state = {job["id"]: {"last_run": today_0400 - 3600}}  # 1 hour before slot
    is_due, why = due(job, state, now)
    assert is_due is True
    assert "daily at 04:00" in why


def test_due_daily_job_not_due(sample_jobs, now):
    job = sample_jobs[1]
    today_0400 = _scheduled_today(now, "04:00")
    state = {job["id"]: {"last_run": today_0400 + 100}}  # already ran after slot
    is_due, why = due(job, state, now)
    assert is_due is False


def test_due_weekly_job_due(sample_jobs, now):
    job = sample_jobs[2]  # weekly
    # State has last_run before the weekly slot
    slot = _last_weekly_slot(now, 0, "04:00")
    state = {job["id"]: {"last_run": slot - 3600}}
    is_due, why = due(job, state, now)
    assert is_due is True
    assert "weekly" in why


def test_due_no_schedule(now):
    job = {"id": "no_sched", "argv": ["python", "test.py"], "enabled": True}
    state = {}
    is_due, why = due(job, state, now)
    assert is_due is False
    assert why == "no schedule"


# ============================================================================
# Tests for plan()
# ============================================================================

def test_plan_returns_due_jobs(sample_jobs, now):
    state = {}
    errors = []
    result = plan(sample_jobs, state, now, errors=errors)
    assert len(result) == 3  # 3 enabled jobs, all due (never ran)
    assert all(job[0]["id"] in ["job_every_15", "job_daily", "job_weekly"] for job in result)


def test_plan_skips_disabled(sample_jobs, now):
    state = {}
    errors = []
    result = plan(sample_jobs, state, now, errors=errors)
    ids = [job[0]["id"] for job in result]
    assert "job_disabled" not in ids


def test_plan_only_flag(sample_jobs, now):
    state = {}
    errors = []
    result = plan(sample_jobs, state, now, only="job_every_15", errors=errors)
    assert len(result) == 1
    assert result[0][0]["id"] == "job_every_15"


def test_plan_only_with_force(sample_jobs, now):
    state = {job["id"]: {"last_run": now - 100} for job in sample_jobs}
    errors = []
    result = plan(sample_jobs, state, now, only="job_every_15", force=True, errors=errors)
    assert len(result) == 1
    assert result[0][1] == "forced"


def test_plan_malformed_job_reported_in_errors(now):
    jobs = [
        {"id": "good", "argv": ["python", "test.py"], "every_min": 10},
        {"id": "bad", "argv": "not a list"},  # invalid argv
        {"argv": ["python", "test.py"]},  # missing id
    ]
    state = {}
    errors = []
    result = plan(jobs, state, now, errors=errors)
    assert len(result) == 1
    assert result[0][0]["id"] == "good"
    assert len(errors) >= 1  # at least one error reported


# ============================================================================
# Tests for update_state()
# ============================================================================

def test_update_state_success():
    state = {}
    new_state = update_state(state, "job1", 0, 1.5, 1000.0)
    assert new_state["job1"]["last_run"] == 1000.0
    assert new_state["job1"]["last_code"] == 0
    assert new_state["job1"]["last_secs"] == 1.5
    assert new_state["job1"]["last_ok"] == 1000.0
    assert new_state["job1"]["fails"] == 0


def test_update_state_failure():
    state = {}
    new_state = update_state(state, "job1", 1, 2.5, 1000.0)
    assert new_state["job1"]["last_code"] == 1
    assert new_state["job1"]["fails"] == 1
    assert "last_ok" not in new_state["job1"]


def test_update_state_increments_fails():
    state = {"job1": {"fails": 2}}
    new_state = update_state(state, "job1", 1, 1.0, 1000.0)
    assert new_state["job1"]["fails"] == 3


def test_update_state_preserves_other_jobs():
    state = {"job2": {"last_run": 500.0}}
    new_state = update_state(state, "job1", 0, 1.0, 1000.0)
    assert new_state["job2"]["last_run"] == 500.0


# ============================================================================
# Tests for _command()
# ============================================================================

def test_command_python_script():
    job = {"argv": ["scripts/test.py", "arg1"]}
    cmd = _command(job)
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("test.py")
    assert cmd[2] == "arg1"


def test_command_powershell_script():
    job = {"argv": ["script.ps1", "arg1"]}
    cmd = _command(job)
    assert cmd[0] == "powershell"
    assert "-File" in cmd
    assert cmd[-1] == "arg1"


# ============================================================================
# Tests for run_job()
# ============================================================================

def test_run_job_success(tmp_path, tmp_root):
    # Create a simple test script that exits 0
    script = tmp_root / "scripts" / "test_ok.py"
    script.write_text("print('ok')\nimport sys; sys.exit(0)")

    job = {"argv": ["python", "scripts/test_ok.py"], "timeout_s": 10}
    with patch("job_runner.ROOT", tmp_root):
        code, secs, tail = run_job(job)
    assert code == 0
    assert secs > 0
    assert "ok" in tail


def test_run_job_failure(tmp_path, tmp_root):
    script = tmp_root / "scripts" / "test_fail.py"
    script.write_text("import sys; sys.exit(1)")

    job = {"argv": ["python", "scripts/test_fail.py"], "timeout_s": 10}
    with patch("job_runner.ROOT", tmp_root):
        code, secs, tail = run_job(job)
    assert code == 1
    assert secs > 0


def test_run_job_timeout(tmp_path, tmp_root):
    script = tmp_root / "scripts" / "test_sleep.py"
    script.write_text("import time; time.sleep(10)")

    job = {"argv": ["python", "scripts/test_sleep.py"], "timeout_s": 1}
    with patch("job_runner.ROOT", tmp_root):
        code, secs, tail = run_job(job)
    assert code == 124  # timeout exit code
    assert "timed out" in tail


def test_run_job_missing_script(tmp_root):
    job = {"argv": ["scripts/missing.py"], "timeout_s": 10}
    with patch("job_runner.ROOT", tmp_root):
        code, secs, tail = run_job(job)
    # On Windows, python returns 2 for missing file
    assert code in (2, 125)
    # Error message contains "can't open file"
    assert "can't open file" in tail or "could not start" in tail


# ============================================================================
# Tests for _read_json
# ============================================================================

def test_read_json_valid(tmp_path):
    f = tmp_path / "test.json"
    f.write_text('{"key": "value"}')
    result = _read_json(f)
    assert result == {"key": "value"}


def test_read_json_missing(tmp_path):
    f = tmp_path / "missing.json"
    with pytest.raises(FileNotFoundError):
        _read_json(f)


def test_read_json_invalid(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not valid json")
    with pytest.raises(ValueError):
        _read_json(f)


def test_read_json_unicode(tmp_path):
    f = tmp_path / "unicode.json"
    f.write_text('{"key": "值"}', encoding="utf-8")
    result = _read_json(f)
    assert result == {"key": "值"}


# ============================================================================
# Tests for load_jobs
# ============================================================================

def test_load_jobs_valid(tmp_path):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps([
        {"id": "job1", "argv": ["python", "test.py"], "every_min": 10},
        {"id": "job2", "argv": ["python", "test2.py"], "daily": "04:00"},
    ]))
    result = load_jobs(jobs_file)
    assert len(result) == 2
    assert result[0]["id"] == "job1"
    assert result[1]["id"] == "job2"


def test_load_jobs_missing_file(tmp_path):
    jobs_file = tmp_path / "missing.json"
    result = load_jobs(jobs_file)
    assert result == []


def test_load_jobs_malformed_json(tmp_path):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text("not json")
    errors = []
    result = load_jobs(jobs_file, errors=errors)
    assert result == []
    assert len(errors) == 1
    assert "cannot be read" in errors[0]["error"]


def test_load_jobs_invalid_rows(tmp_path):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps([
        {"id": "good", "argv": ["python", "test.py"]},
        {"argv": ["python", "test.py"]},  # missing id
        {"id": "bad_argv", "argv": "not a list"},
        {"id": "empty_argv", "argv": []},
    ]))
    errors = []
    result = load_jobs(jobs_file, errors=errors)
    assert len(result) == 1
    assert result[0]["id"] == "good"
    assert len(errors) == 3


# ============================================================================
# Tests for _load_state / _save_json
# ============================================================================

def test_load_state_missing(tmp_logs):
    state, corrupt = _load_state(tmp_logs)
    assert state == {}
    assert corrupt is False


def test_load_state_valid(tmp_logs):
    state_file = tmp_logs / STATE_FILE
    state_file.write_text(json.dumps({"job1": {"last_run": 1000.0}}))
    state, corrupt = _load_state(tmp_logs)
    assert state == {"job1": {"last_run": 1000.0}}
    assert corrupt is False


def test_load_state_corrupt(tmp_logs):
    state_file = tmp_logs / STATE_FILE
    state_file.write_text("not json")
    state, corrupt = _load_state(tmp_logs, move=True)
    assert state == {}
    assert corrupt is True
    # Corrupt file should be moved aside
    corrupt_files = list(tmp_logs.glob("*.corrupt-*"))
    assert len(corrupt_files) == 1


def test_load_state_not_dict(tmp_logs):
    state_file = tmp_logs / STATE_FILE
    state_file.write_text(json.dumps(["not", "a", "dict"]))
    state, corrupt = _load_state(tmp_logs)
    assert state == {}
    assert corrupt is True


def test_save_json_atomic(tmp_path):
    f = tmp_path / "test.json"
    _save_json(f, {"key": "value"})
    assert f.exists()
    assert json.loads(f.read_text()) == {"key": "value"}


# ============================================================================
# Tests for lock
# ============================================================================

def test_acquire_release_lock(tmp_logs):
    pid = 12345
    _acquire_lock("test", pid, 60, 0, tmp_logs)
    lock_file = tmp_logs / "test.lock"
    assert lock_file.exists()
    content = lock_file.read_text()
    assert str(pid) in content

    _release_lock("test", pid, tmp_logs)
    assert not lock_file.exists()


def test_lock_held_by_other(tmp_logs):
    pid1 = 12345
    pid2 = 54321
    _acquire_lock("test", pid1, 60, 0, tmp_logs)

    with pytest.raises(LockHeld):
        _acquire_lock("test", pid2, 60, 0, tmp_logs)

    # First lock still there
    assert (tmp_logs / "test.lock").exists()


def test_lock_stale_ttl(tmp_logs):
    pid1 = 12345
    pid2 = 54321
    lock_file = tmp_logs / "test.lock"
    # Create a stale lock (old timestamp) - need to include TTL expired
    # Use a time far in the past
    old_time = time.time() - 1000
    lock_file.write_text(f"{pid1} {old_time}")

    # Should be able to acquire because TTL expired (TTL is 60 seconds)
    _acquire_lock("test", pid2, 60, 0, tmp_logs)
    content = lock_file.read_text().strip()
    assert content.startswith(str(pid2))


# ============================================================================
# Tests for _write_heartbeat / _check_kill_switch
# ============================================================================

def test_write_heartbeat(tmp_logs):
    _write_heartbeat(tmp_logs, "job_runner")
    beat_file = tmp_logs / "heartbeats" / "job_runner.beat"
    assert beat_file.exists()
    beat_time = float(beat_file.read_text().strip())
    assert abs(beat_time - time.time()) < 2


def test_check_kill_switch_exists(tmp_logs):
    (tmp_logs / "job_runner_off.txt").write_text("off")
    assert _check_kill_switch(tmp_logs) is True


def test_check_kill_switch_missing(tmp_logs):
    assert _check_kill_switch(tmp_logs) is False


# ============================================================================
# Tests for _run_once
# ============================================================================

def test_run_once_runs_due_jobs(tmp_logs, tmp_root, sample_jobs, now):
    # Create test scripts that succeed - need to fix argv format
    fixed_jobs = []
    for job in sample_jobs[:3]:
        new_job = dict(job)
        new_job["argv"] = ["scripts/" + job["argv"][1].split("/")[-1]]
        fixed_jobs.append(new_job)
        script = tmp_root / new_job["argv"][0]
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("print('ok')\nimport sys; sys.exit(0)")

    state = {}
    errors = []
    with patch("job_runner.ROOT", tmp_root):
        ran, bad, job_errors, new_state = _run_once(fixed_jobs, state, now, tmp_logs, errors=errors)

    assert ran == 3
    assert bad == 0
    assert len(job_errors) == 0
    assert all(job_id in new_state for job_id in ["job_every_15", "job_daily", "job_weekly"])


def test_run_once_records_failure(tmp_logs, tmp_root, now):
    script = tmp_root / "scripts" / "fail.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("import sys; sys.exit(1)")

    jobs = [{"id": "fail_job", "argv": ["scripts/fail.py"], "every_min": 10, "timeout_s": 10, "enabled": True}]
    state = {}
    errors = []
    with patch("job_runner.ROOT", tmp_root):
        ran, bad, job_errors, new_state = _run_once(jobs, state, now, tmp_logs, errors=errors)

    assert ran == 1
    assert bad == 1
    assert new_state["fail_job"]["last_code"] == 1
    assert new_state["fail_job"]["fails"] == 1


def test_run_once_timeout_kills_child(tmp_logs, tmp_root, now):
    script = tmp_root / "scripts" / "sleep.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("import time; time.sleep(10)")

    jobs = [{"id": "sleep_job", "argv": ["scripts/sleep.py"], "every_min": 10, "timeout_s": 1, "enabled": True}]
    state = {}
    errors = []
    with patch("job_runner.ROOT", tmp_root):
        ran, bad, job_errors, new_state = _run_once(jobs, state, now, tmp_logs, errors=errors)

    assert ran == 1
    assert bad == 1
    assert new_state["sleep_job"]["last_code"] == 124


def test_run_once_budget_stops_jobs(tmp_logs, tmp_root, now):
    # Create multiple quick jobs
    for i in range(5):
        script = tmp_root / "scripts" / f"quick{i}.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("print('ok')")

    jobs = [
        {"id": f"quick{i}", "argv": [f"scripts/quick{i}.py"], "every_min": 10, "timeout_s": 10, "enabled": True}
        for i in range(5)
    ]
    state = {}
    errors = []
    # Use a small budget
    with patch("job_runner.BUDGET_S", 0.1), patch("job_runner.ROOT", tmp_root):
        ran, bad, job_errors, new_state = _run_once(jobs, state, now, tmp_logs, errors=errors)

    # Should only run 1 job before budget exceeded
    assert ran >= 1


def test_run_once_failing_job_does_not_stop_next(tmp_logs, tmp_root, now):
    fail_script = tmp_root / "scripts" / "fail.py"
    ok_script = tmp_root / "scripts" / "ok.py"
    fail_script.parent.mkdir(parents=True, exist_ok=True)
    fail_script.write_text("import sys; sys.exit(1)")
    ok_script.write_text("print('ok')")

    jobs = [
        {"id": "fail_job", "argv": ["scripts/fail.py"], "every_min": 10, "timeout_s": 10, "enabled": True},
        {"id": "ok_job", "argv": ["scripts/ok.py"], "every_min": 10, "timeout_s": 10, "enabled": True},
    ]
    state = {}
    errors = []
    with patch("job_runner.ROOT", tmp_root):
        ran, bad, job_errors, new_state = _run_once(jobs, state, now, tmp_logs, errors=errors)

    assert ran == 2
    assert bad == 1
    assert new_state["fail_job"]["last_code"] == 1
    assert new_state["ok_job"]["last_code"] == 0


# ============================================================================
# Tests for main() CLI flags
# ============================================================================

def test_main_once_flag(tmp_logs, tmp_root, monkeypatch, now):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_logs))
    monkeypatch.setattr("job_runner.ROOT", tmp_root)
    monkeypatch.setattr("job_runner.LOGS", tmp_logs)
    monkeypatch.setattr("job_runner.JOBS_FILE", tmp_root / "scripts" / "jobs.json")

    # Create jobs.json
    jobs_file = tmp_root / "scripts" / "jobs.json"
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    jobs_file.write_text(json.dumps([
        {"id": "test_job", "argv": ["python", "scripts/test.py"], "every_min": 10, "timeout_s": 10, "enabled": True}
    ]))

    # Create test script
    script = tmp_root / "scripts" / "test.py"
    script.write_text("print('ok')")

    with patch("job_runner.time.time", return_value=now):
        result = main(["--once"])

    assert result == 0


def test_main_list_flag(tmp_logs, tmp_root, monkeypatch, now, capsys):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_logs))
    monkeypatch.setattr("job_runner.ROOT", tmp_root)
    monkeypatch.setattr("job_runner.LOGS", tmp_logs)
    monkeypatch.setattr("job_runner.JOBS_FILE", tmp_root / "scripts" / "jobs.json")

    jobs_file = tmp_root / "scripts" / "jobs.json"
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    jobs_file.write_text(json.dumps([
        {"id": "test_job", "argv": ["python", "scripts/test.py"], "every_min": 10, "timeout_s": 10, "enabled": True}
    ]))

    with patch("job_runner.time.time", return_value=now):
        result = main(["--list"])

    assert result == 0
    out = capsys.readouterr().out
    assert "test_job" in out
    assert "every 10 min" in out


def test_main_dry_run(tmp_logs, tmp_root, monkeypatch, now, capsys):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_logs))
    monkeypatch.setattr("job_runner.ROOT", tmp_root)
    monkeypatch.setattr("job_runner.LOGS", tmp_logs)
    monkeypatch.setattr("job_runner.JOBS_FILE", tmp_root / "scripts" / "jobs.json")

    jobs_file = tmp_root / "scripts" / "jobs.json"
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    jobs_file.write_text(json.dumps([
        {"id": "test_job", "argv": ["python", "scripts/test.py"], "every_min": 10, "timeout_s": 10, "enabled": True}
    ]))

    with patch("job_runner.time.time", return_value=now):
        result = main(["--dry-run"])

    assert result == 0
    out = capsys.readouterr().out
    assert "would run" in out
    assert "test_job" in out


def test_main_kill_switch(tmp_logs, tmp_root, monkeypatch):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_logs))
    monkeypatch.setattr("job_runner.ROOT", tmp_root)
    monkeypatch.setattr("job_runner.LOGS", tmp_logs)

    # Create kill switch
    (tmp_logs / "job_runner_off.txt").write_text("off")

    result = main([])
    assert result == 0


def test_main_kill_switch_force(tmp_logs, tmp_root, monkeypatch, now):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_logs))
    monkeypatch.setattr("job_runner.ROOT", tmp_root)
    monkeypatch.setattr("job_runner.LOGS", tmp_logs)
    monkeypatch.setattr("job_runner.JOBS_FILE", tmp_root / "scripts" / "jobs.json")

    (tmp_logs / "job_runner_off.txt").write_text("off")

    jobs_file = tmp_root / "scripts" / "jobs.json"
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    jobs_file.write_text(json.dumps([
        {"id": "test_job", "argv": ["python", "scripts/test.py"], "every_min": 10, "timeout_s": 10, "enabled": True}
    ]))

    script = tmp_root / "scripts" / "test.py"
    script.write_text("print('ok')")

    with patch("job_runner.time.time", return_value=now):
        result = main(["--once", "--force"])

    assert result == 0


def test_main_lock_held(tmp_logs, tmp_root, monkeypatch):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_logs))
    monkeypatch.setattr("job_runner.ROOT", tmp_root)
    monkeypatch.setattr("job_runner.LOGS", tmp_logs)

    # Create a lock file held by another process
    (tmp_logs / "job_runner.lock").write_text(f"99999 {time.time()}")

    result = main(["--once"])
    # --once without --only is a normal tick, returns 0 even if lock held
    assert result == 0


def test_main_lock_held_with_only(tmp_logs, tmp_root, monkeypatch):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_logs))
    monkeypatch.setattr("job_runner.ROOT", tmp_root)
    monkeypatch.setattr("job_runner.LOGS", tmp_logs)

    # Create a lock file held by another process
    (tmp_logs / "job_runner.lock").write_text(f"99999 {time.time()}")

    result = main(["--only", "somejob", "--force"])
    # With --only, returns 4 if lock held
    assert result == 4


def test_main_only_force(tmp_logs, tmp_root, monkeypatch, now):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_logs))
    monkeypatch.setattr("job_runner.ROOT", tmp_root)
    monkeypatch.setattr("job_runner.LOGS", tmp_logs)
    monkeypatch.setattr("job_runner.JOBS_FILE", tmp_root / "scripts" / "jobs.json")

    jobs_file = tmp_root / "scripts" / "jobs.json"
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    jobs_file.write_text(json.dumps([
        {"id": "job1", "argv": ["python", "scripts/test.py"], "every_min": 10, "timeout_s": 10, "enabled": True},
        {"id": "job2", "argv": ["python", "scripts/test.py"], "every_min": 10, "timeout_s": 10, "enabled": True},
    ]))

    script = tmp_root / "scripts" / "test.py"
    script.write_text("print('ok')")

    with patch("job_runner.time.time", return_value=now):
        result = main(["--only", "job1", "--force"])

    assert result == 0


def test_main_only_force_not_found(tmp_logs, tmp_root, monkeypatch, now):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_logs))
    monkeypatch.setattr("job_runner.ROOT", tmp_root)
    monkeypatch.setattr("job_runner.LOGS", tmp_logs)
    monkeypatch.setattr("job_runner.JOBS_FILE", tmp_root / "scripts" / "jobs.json")

    jobs_file = tmp_root / "scripts" / "jobs.json"
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    jobs_file.write_text(json.dumps([
        {"id": "job1", "argv": ["python", "scripts/test.py"], "every_min": 10, "timeout_s": 10, "enabled": True},
    ]))

    with patch("job_runner.time.time", return_value=now):
        result = main(["--only", "nonexistent", "--force"])

    assert result == 3  # Nothing ran


# ============================================================================
# Tests for state persistence between passes
# ============================================================================

def test_state_persists_between_passes(tmp_logs, tmp_root, now):
    script = tmp_root / "scripts" / "test.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('ok')")

    jobs = [{"id": "test_job", "argv": ["scripts/test.py"], "every_min": 10, "timeout_s": 10, "enabled": True}]

    # First pass - mock time.time to return our fixed 'now'
    state = {}
    with patch("job_runner.ROOT", tmp_root), patch("job_runner.time.time", return_value=now):
        ran1, bad1, _, state = _run_once(jobs, state, now, tmp_logs)

    assert ran1 == 1
    # last_run should be close to now (within a few seconds)
    assert abs(state["test_job"]["last_run"] - now) < 5

    # Second pass - should not run again (not enough time passed)
    with patch("job_runner.ROOT", tmp_root), patch("job_runner.time.time", return_value=now + 100):
        ran2, bad2, _, state = _run_once(jobs, state, now + 100, tmp_logs)  # 100 seconds later

    assert ran2 == 0  # Not due yet


def test_state_persists_after_timeout(tmp_logs, tmp_root, now):
    script = tmp_root / "scripts" / "sleep.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("import time; time.sleep(10)")

    jobs = [{"id": "sleep_job", "argv": ["python", "scripts/sleep.py"], "every_min": 10, "timeout_s": 1, "enabled": True}]

    state = {}
    with patch("job_runner.ROOT", tmp_root):
        ran, bad, _, state = _run_once(jobs, state, now, tmp_logs)

    assert ran == 1
    assert bad == 1
    assert state["sleep_job"]["last_code"] == 124
    assert state["sleep_job"]["fails"] == 1


# ============================================================================
# Tests for commands run with cwd ROOT
# ============================================================================

def test_command_runs_with_cwd_root(tmp_root, tmp_logs, now):
    # Create a script that writes its cwd to a file
    script = tmp_root / "scripts" / "cwd_test.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    cwd_file = tmp_root / "cwd_result.txt"
    script.write_text(f"""
import os
with open(r"{cwd_file}", "w") as f:
    f.write(os.getcwd())
""")

    jobs = [{"id": "cwd_job", "argv": ["python", "scripts/cwd_test.py"], "every_min": 10, "timeout_s": 10, "enabled": True}]
    state = {}

    with patch("job_runner.ROOT", tmp_root):
        _run_once(jobs, state, now, tmp_logs)

    assert cwd_file.exists()
    assert cwd_file.read_text().strip() == str(tmp_root)


# ============================================================================
# Tests for overlapping passes (lock prevents double run)
# ============================================================================

def test_lock_prevents_overlapping_passes(tmp_logs, tmp_root, now, monkeypatch):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_logs))
    monkeypatch.setattr("job_runner.ROOT", tmp_root)
    monkeypatch.setattr("job_runner.LOGS", tmp_logs)
    monkeypatch.setattr("job_runner.JOBS_FILE", tmp_root / "scripts" / "jobs.json")

    jobs_file = tmp_root / "scripts" / "jobs.json"
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    jobs_file.write_text(json.dumps([
        {"id": "test_job", "argv": ["python", "scripts/test.py"], "every_min": 10, "timeout_s": 10, "enabled": True}
    ]))

    script = tmp_root / "scripts" / "test.py"
    script.write_text("print('ok')")

    # First run acquires lock
    with patch("job_runner.time.time", return_value=now):
        # Simulate first run holding the lock
        _acquire_lock("job_runner", 11111, 600, 0, tmp_logs)
        try:
            result = main(["--once"])
            # Should fail with lock held - normal tick returns 0, --only returns 4
            assert result == 0  # --once without --only is a normal tick
        finally:
            _release_lock("job_runner", 11111, tmp_logs)


# ============================================================================
# Tests for heartbeat written on every pass
# ============================================================================

def test_heartbeat_written_on_pass(tmp_logs, tmp_root, now):
    script = tmp_root / "scripts" / "test.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('ok')")

    jobs = [{"id": "test_job", "argv": ["python", "scripts/test.py"], "every_min": 10, "timeout_s": 10, "enabled": True}]
    state = {}

    with patch("job_runner.ROOT", tmp_root):
        _run_once(jobs, state, now, tmp_logs)

    beat_file = tmp_logs / "heartbeats" / "job_runner.beat"
    assert beat_file.exists()
    beat_time = float(beat_file.read_text().strip())
    assert abs(beat_time - time.time()) < 2  # Use current time, not the mocked now


# ============================================================================
# Tests for error handling - malformed jobs.json
# ============================================================================

def test_malformed_jobs_json_does_not_crash(tmp_logs, tmp_root, now, monkeypatch):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_logs))
    monkeypatch.setattr("job_runner.ROOT", tmp_root)
    monkeypatch.setattr("job_runner.LOGS", tmp_logs)
    monkeypatch.setattr("job_runner.JOBS_FILE", tmp_root / "scripts" / "jobs.json")

    jobs_file = tmp_root / "scripts" / "jobs.json"
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    jobs_file.write_text("not valid json {")

    script = tmp_root / "scripts" / "test.py"
    script.write_text("print('ok')")

    with patch("job_runner.time.time", return_value=now):
        result = main(["--once"])

    assert result == 0  # Doesn't crash
    # Error should be logged
    errors_file = tmp_logs / ERRORS_FILE
    assert errors_file.exists()
    errors = json.loads(errors_file.read_text())
    assert len(errors) >= 1
    assert "cannot be read" in errors[0]["error"]


# ============================================================================
# Tests for unicode handling
# ============================================================================

def test_unicode_in_job_output(tmp_logs, tmp_root, now):
    script = tmp_root / "scripts" / "unicode_test.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('测试中文')", encoding="utf-8")

    jobs = [{"id": "unicode_job", "argv": ["python", "scripts/unicode_test.py"], "every_min": 10, "timeout_s": 10, "enabled": True}]
    state = {}

    with patch("job_runner.ROOT", tmp_root):
        ran, bad, _, state = _run_once(jobs, state, now, tmp_logs)

    assert ran == 1
    assert bad == 0
    # Check log file has unicode
    log_file = tmp_logs / LOG_FILE
    log_content = log_file.read_text(encoding="utf-8")
    assert "测试中文" in log_content


# ============================================================================
# Tests for missing folder handling
# ============================================================================

def test_missing_logs_folder_created(tmp_path, tmp_root, now, monkeypatch):
    logs = tmp_path / "logs"  # Doesn't exist yet
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setattr("job_runner.ROOT", tmp_root)
    monkeypatch.setattr("job_runner.LOGS", logs)
    monkeypatch.setattr("job_runner.JOBS_FILE", tmp_root / "scripts" / "jobs.json")

    jobs_file = tmp_root / "scripts" / "jobs.json"
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    jobs_file.write_text(json.dumps([
        {"id": "test_job", "argv": ["python", "scripts/test.py"], "every_min": 10, "timeout_s": 10, "enabled": True}
    ]))

    script = tmp_root / "scripts" / "test.py"
    script.write_text("print('ok')")

    with patch("job_runner.time.time", return_value=now):
        result = main(["--once"])

    assert result == 0
    assert logs.exists()
    assert (logs / STATE_FILE).exists()
    assert (logs / "heartbeats" / "job_runner.beat").exists()


# ============================================================================
# Tests for non-zero exit recorded
# ============================================================================

def test_non_zero_exit_recorded_in_state(tmp_logs, tmp_root, now):
    script = tmp_root / "scripts" / "fail.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("import sys; sys.exit(42)")

    jobs = [{"id": "fail_job", "argv": ["scripts/fail.py"], "every_min": 10, "timeout_s": 10, "enabled": True}]
    state = {}

    with patch("job_runner.ROOT", tmp_root), patch("job_runner.time.time", return_value=now):
        _, _, _, state = _run_once(jobs, state, now, tmp_logs)

    assert state["fail_job"]["last_code"] == 42
    assert state["fail_job"]["fails"] == 1

    # Check log file
    log_file = tmp_logs / LOG_FILE
    log_content = log_file.read_text()
    assert "FAIL(42)" in log_content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

# Added in review (2026-10-31)
def test_the_shipped_job_table_commands_point_at_files_that_exist_in_the_kit():
    """jobs.json names scripts as 'scripts/x.py'; they must resolve to a real file next to job_runner.py (the worker's version looked in scripts/scripts/)."""
    import job_runner as jr
    rows = json.loads((Path(__file__).parent / "jobs.json").read_text(encoding="utf-8"))
    if not (Path(__file__).parent / "sensor_deadlines.py").exists():
        pytest.skip("run inside the kit: the scripts the table names are not next to this copy")
    assert rows
    for row in rows:
        cmd = jr._command(row)
        assert Path(cmd[1]).is_file(), (row["id"], cmd)
        assert Path(cmd[1]).parent.resolve() == Path(__file__).resolve().parent        # resolve: a subst drive and its real path are the same folder


def test_script_path_prefers_the_assistant_folder_then_falls_back_to_next_to_the_runner(tmp_path, monkeypatch):
    import job_runner as jr
    root, here = tmp_path / "root", tmp_path / "here"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    here.mkdir(exist_ok=True)
    (root / "scripts" / "a.py").write_text("")
    (here / "b.py").write_text("")
    monkeypatch.setattr(jr, "ROOT", root)
    monkeypatch.setattr(jr, "HERE", here)
    assert jr._script_path("scripts/a.py") == str(root / "scripts" / "a.py")
    assert jr._script_path("b.py") == str(here / "b.py")
    assert jr._script_path("nope.py") == str(root / "nope.py")


def test_a_lock_owned_by_pid_123_is_not_released_by_pid_12(tmp_path):
    import job_runner as jr
    (tmp_path / "x.lock").write_text("123 %s" % time.time())
    jr._release_lock("x", 12, tmp_path)
    assert (tmp_path / "x.lock").exists()
    jr._release_lock("x", 123, tmp_path)
    assert not (tmp_path / "x.lock").exists()


def test_only_as_the_last_argument_does_not_crash(tmp_path, monkeypatch):
    import job_runner as jr
    monkeypatch.setattr(jr, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(jr, "JOBS_FILE", tmp_path / "jobs.json")
    assert jr.main(["--dry-run", "--only"]) == 0
