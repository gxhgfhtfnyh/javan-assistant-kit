# -*- coding: utf-8 -*-
"""Tests for sensor_runner.py: discovery, planning, failure isolation, the two-failures alarm and a real run of tiny fake sensors."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import intent_store  # noqa: E402
import sensor_runner as sr  # noqa: E402

NOW = 1_800_000_000.0


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A private assistant folder with its own (empty) scripts folder and logs."""
    logs, scripts = tmp_path / "logs", tmp_path / "scripts"
    logs.mkdir()
    scripts.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setattr(sr, "SCRIPTS", scripts)
    return tmp_path


def touch_sensors(box, *names):
    for n in names:
        (box / "scripts" / (n + ".py")).write_text("# fake\n", encoding="utf-8")


# ---------- pure core ----------
def test_discover_finds_sensors_and_the_named_extra_and_ignores_tests_and_dupes():
    files = ["sense_self.py", "/x/y/sense_health.py", "test_sense_self.py", "sensor_runner.py", "sense_self.py", "sense_x.txt", "readme.md", "sensor_deadlines.py",
             "C:\\s\\sense_win.py", "sense_bad name.py", "sense_.py"]
    assert sr.discover(files) == ["sense_", "sense_health", "sense_self", "sense_win", "sensor_deadlines"]
    assert sr.discover(files, extra=()) == ["sense_", "sense_health", "sense_self", "sense_win"]
    assert sr.discover(None) == [] and sr.discover([]) == []


def test_scheduled_elsewhere_lists_only_enabled_rows_and_survives_junk():
    jobs = [{"id": "a", "argv": ["python", "scripts/sensor_deadlines.py"], "every_min": 15},
            {"id": "b", "argv": ["python", "scripts/sense_initiative.py"], "every_min": 30, "enabled": False},
            {"id": "c", "argv": ["scripts/sense_x.py", "--flag"], "every_min": 5}, {"id": "d", "argv": "nope"}, "junk", {"id": "e", "argv": ["echo", "hi"]}]
    assert sr.scheduled_elsewhere(jobs) == {"sensor_deadlines", "sense_x"}
    assert sr.scheduled_elsewhere(None) == set() and sr.scheduled_elsewhere({"a": 1}) == set()


def test_plan_runs_everything_that_has_not_run_recently():
    state = {"sense_a": {"last_run": NOW - 100}, "sense_b": {"last_run": NOW - 5000}}
    assert sr.plan(["sense_a", "sense_b", "sense_c"], state, NOW) == ["sense_b", "sense_c"]       # a is inside the gap, c never ran


def test_force_and_only_work_together_and_alone_and_a_damaged_state_means_never_ran():
    state = {"sense_a": {"last_run": NOW - 100}}
    assert sr.plan(["sense_a", "sense_b"], state, NOW, force=True) == ["sense_a", "sense_b"]
    assert sr.plan(["sense_a", "sense_b"], state, NOW, force=True, only="sense_a") == ["sense_a"]
    assert sr.plan(["sense_a", "sense_b"], state, NOW, only="sense_a") == []                      # not forced, still inside the gap
    assert sr.plan(["sense_a"], {"sense_a": {"last_run": "yesterday"}}, NOW) == ["sense_a"]


def test_parse_counts_reads_the_summary_line():
    assert sr.parse_counts("sense_health: 3 finding(s), 3 new intent(s)") == (3, 3)
    assert sr.parse_counts("sense_self: 1 finding(s), 0 new intent(s)") == (1, 0)
    assert sr.parse_counts("sense_self: off") == (None, None) and sr.parse_counts("") == (None, None) and sr.parse_counts(None) == (None, None)


def test_update_state_counts_consecutive_failures_resets_on_success_and_does_not_mutate():
    s0 = {}
    s = sr.update_state(s0, "sense_a", False, NOW)
    s = sr.update_state(s, "sense_a", False, NOW + 1)
    assert s["sense_a"]["fails"] == 2 and s["sense_a"]["last_run"] == NOW + 1
    assert sr.update_state(s, "sense_a", True, NOW + 2)["sense_a"]["fails"] == 0
    assert s0 == {} and sr.update_state({"x": {"fails": "many"}}, "x", False, NOW)["x"]["fails"] == 1


def test_the_alarm_comes_on_the_second_failure_and_then_about_daily():
    assert [n for n in range(1, 300) if sr.should_alarm(n)] == [2, 96, 192, 288]
    assert not sr.should_alarm(0)


# ---------- running one sensor ----------
def test_run_one_reports_a_timeout_a_crash_and_a_clean_run(monkeypatch):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("x", 1)
    monkeypatch.setattr(sr.subprocess, "run", timeout)
    ok, _s, f, n, note = sr.run_one("sense_a", timeout=1)
    assert ok is False and f is None and "timed out" in note

    def boom(*a, **k):
        raise OSError("no python")
    monkeypatch.setattr(sr.subprocess, "run", boom)
    ok, *_rest, note = sr.run_one("sense_a")
    assert ok is False and "could not start" in note

    class R:
        returncode = 0
        stdout = "sense_a: 2 finding(s), 1 new intent(s)\n"
        stderr = ""
    monkeypatch.setattr(sr.subprocess, "run", lambda *a, **k: R())
    res = sr.run_one("sense_a")
    assert res[0] is True and res[2:4] == (2, 1)


def test_run_one_reports_a_nonzero_exit_with_the_last_error_line(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
        stderr = "Traceback...\nValueError: bad data\n"
    monkeypatch.setattr(sr.subprocess, "run", lambda *a, **k: R())
    ok, _s, _f, _n, note = sr.run_one("sense_a")
    assert ok is False and "exit 1: ValueError: bad data" in note


def test_run_one_refuses_a_name_that_is_not_a_plain_sensor_name(monkeypatch):
    monkeypatch.setattr(sr.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not start")))
    for bad in ("../evil", "a b", "x;y", "", "a/b"):
        ok, _s, _f, _n, note = sr.run_one(bad)
        assert ok is False and note == "bad sensor name"


# ---------- whole runs ----------
def _fake(monkeypatch, results):
    monkeypatch.setattr(sr, "run_one", lambda name, timeout=sr.TIMEOUT: results[name])


def test_one_failing_sensor_does_not_stop_the_others_and_is_logged(box, monkeypatch, capsys):
    touch_sensors(box, "sense_a", "sense_b", "sense_c", "test_sense_a")
    _fake(monkeypatch, {"sense_a": (True, 0.1, 1, 1, ""), "sense_b": (False, 0.2, None, None, "timed out after 90s"), "sense_c": (True, 0.1, 0, 0, "")})
    assert sr.main([]) == 0
    assert "ran 3 of 3 sensor(s), 0 failing repeatedly" in capsys.readouterr().out
    log = (box / "logs" / sr.LOG_NAME).read_text(encoding="utf-8")
    assert log.count("\n") == 3 and "sense_b" in log and "FAIL" in log and "timed out" in log
    assert sr._load_state(box / "logs")["sense_b"]["fails"] == 1
    assert (box / "logs" / sr.LAST_NAME).read_text(encoding="utf-8").isdigit()


def test_a_sensor_failing_twice_raises_one_work_item_and_then_no_more(box, monkeypatch, capsys):
    touch_sensors(box, "sense_b")
    _fake(monkeypatch, {"sense_b": (False, 0.2, None, None, "timed out after 90s")})
    logs = str(box / "logs")
    sr.main(["--force"])
    assert intent_store.read_jsonl(intent_store.INTENTS, logs) == []                # first failure: only logged
    assert not (box / "logs" / "alerts.txt").exists()
    sr.main(["--force"])
    intents = intent_store.read_jsonl(intent_store.INTENTS, logs)
    assert len(intents) == 1 and intents[0]["kind"] == "sensor_failing" and intents[0]["value"] == 4 and intents[0]["tier"] == 1
    assert intents[0]["what"].startswith("sensor sense_b failed twice in a row: timed out")
    assert "1 failing repeatedly" in capsys.readouterr().out.splitlines()[-1]
    sr.main(["--force"])
    sr.main(["--force"])
    assert len(intent_store.read_jsonl(intent_store.INTENTS, logs)) == 1             # not raised again
    assert (box / "logs" / "alerts.txt").read_text(encoding="utf-8").count("sense_b failed twice") == 1


def test_a_recovered_sensor_starts_counting_again(box, monkeypatch):
    touch_sensors(box, "sense_b")
    state = {}
    for ok in (False, True, False):
        _fake(monkeypatch, {"sense_b": (ok, 0.1, None, None, "" if ok else "exit 1: x")})
        sr.main(["--force"])
        state = sr._load_state(box / "logs")
    assert state["sense_b"]["fails"] == 1 and intent_store.read_jsonl(intent_store.INTENTS, str(box / "logs")) == []


def test_a_sensor_with_its_own_job_row_is_left_alone_unless_all_or_only_is_given(box, monkeypatch, capsys):
    touch_sensors(box, "sense_a", "sensor_deadlines")
    (box / "scripts" / "jobs.json").write_text(json.dumps([{"id": "d", "argv": ["python", "scripts/sensor_deadlines.py"], "every_min": 15}]), encoding="utf-8")
    ran = []
    monkeypatch.setattr(sr, "run_one", lambda name, timeout=sr.TIMEOUT: (ran.append(name) or (True, 0.1, 0, 0, "")))
    sr.main([])
    assert ran == ["sense_a"] and "ran 1 of 1 sensor(s)" in capsys.readouterr().out
    ran.clear()
    sr.main(["--all", "--force"])
    assert sorted(ran) == ["sense_a", "sensor_deadlines"]
    ran.clear()
    sr.main(["--only", "sensor_deadlines", "--force"])
    assert ran == ["sensor_deadlines"]


def test_dry_run_runs_nothing_and_writes_nothing(box, monkeypatch, capsys):
    touch_sensors(box, "sense_a", "sensor_deadlines")
    (box / "scripts" / "jobs.json").write_text(json.dumps([{"id": "d", "argv": ["python", "scripts/sensor_deadlines.py"], "every_min": 15}]), encoding="utf-8")
    monkeypatch.setattr(sr, "run_one", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert sr.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would run now: sense_a" in out and "left to the job runner (own job row): sensor_deadlines" in out
    assert os.listdir(box / "logs") == []


def test_kill_switch_stops_the_pass(box, monkeypatch, capsys):
    touch_sensors(box, "sense_a")
    (box / "logs" / sr.OFF_NAME).write_text("off", encoding="utf-8")
    monkeypatch.setattr(sr, "run_one", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert sr.main([]) == 0 and "off" in capsys.readouterr().out
    assert sorted(os.listdir(box / "logs")) == [sr.OFF_NAME]


def test_an_unknown_only_name_runs_nothing_and_says_so(box, monkeypatch, capsys):
    touch_sensors(box, "sense_a")
    monkeypatch.setattr(sr, "run_one", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert sr.main(["--only", "../../evil", "--force"]) == 0
    assert "ran 0 of 1 sensor(s), 0 failing repeatedly" in capsys.readouterr().out


def test_the_time_budget_leaves_the_remaining_sensors_for_the_next_pass(box, monkeypatch, capsys):
    touch_sensors(box, "sense_a", "sense_b", "sense_c")
    monkeypatch.setattr(sr, "BUDGET_S", -1)
    ran = []
    monkeypatch.setattr(sr, "run_one", lambda name, timeout=sr.TIMEOUT: (ran.append(name) or (True, 0.1, 0, 0, "")))
    sr.main([])
    assert ran == ["sense_a"]                                                      # at least one always runs, the rest wait
    assert "ran 1 of 3" in capsys.readouterr().out
    assert set(sr._load_state(box / "logs")) == {"sense_a"}


def test_the_log_is_trimmed_when_it_grows_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "LOG_MAX_BYTES", 500)
    monkeypatch.setattr(sr, "LOG_KEEP_LINES", 5)
    p = tmp_path / "sensors.log"
    sr._append_log(p, ["line %03d " % i + "x" * 40 for i in range(30)])
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5 and lines[-1].startswith("line 029")


def test_a_damaged_state_file_is_treated_as_a_fresh_start(box):
    (box / "logs" / sr.STATE_NAME).write_text("{ nope", encoding="utf-8")
    assert sr._load_state(box / "logs") == {}
    (box / "logs" / sr.STATE_NAME).write_text(json.dumps({"a": {"fails": 1}, "b": 5}), encoding="utf-8")
    assert sr._load_state(box / "logs") == {"a": {"fails": 1}}


def test_a_real_pass_over_real_fake_sensors_in_a_private_folder(tmp_path):
    """The real program, the real intent store and notify, three tiny sensors: one fine, one that crashes, one that hangs past a short limit is covered above."""
    root = tmp_path / "assistant"
    scripts, logs = root / "scripts", root / "logs"
    scripts.mkdir(parents=True)
    for name in ("sensor_runner.py", "intent_store.py", "notify.py"):
        shutil.copy(HERE / name, scripts / name)
    (scripts / "sense_good.py").write_text("print('sense_good: 2 finding(s), 1 new intent(s)')\n", encoding="utf-8")
    (scripts / "sense_bad.py").write_text("import sys\nsys.stderr.write('Boom: it broke\\n')\nsys.exit(3)\n", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(logs), PYTHONIOENCODING="utf-8")
    outs = [subprocess.run([sys.executable, str(scripts / "sensor_runner.py"), "--force"], env=env, capture_output=True, text=True, timeout=60) for _ in range(2)]
    assert all(o.returncode == 0 for o in outs)
    assert "ran 2 of 2 sensor(s), 0 failing repeatedly" in outs[0].stdout and "ran 2 of 2 sensor(s), 1 failing repeatedly" in outs[1].stdout
    log = (logs / "sensors.log").read_text(encoding="utf-8")
    assert "findings=2 new=1" in log and "exit 3: Boom: it broke" in log
    intents = intent_store.read_jsonl(intent_store.INTENTS, str(logs))
    assert [i["kind"] for i in intents] == ["sensor_failing"] and "sense_bad" in intents[0]["what"]
    assert (logs / "sensor_last.txt").exists() and time.time() - int((logs / "sensor_last.txt").read_text(encoding="utf-8")) < 120
