# -*- coding: utf-8 -*-
"""Tests for sense_health.py: the pure rules, the readers (all faked) and the whole run on a temporary folder."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import intent_store  # noqa: E402
import sense_health as sh  # noqa: E402

NOW = 1_800_000_000.0
DAEMON = "listener.py"


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A private assistant folder: its own logs, and an empty scripts folder so the real jobs.json is never read."""
    logs = tmp_path / "logs"
    logs.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.delenv("HARNESS_TASK_PREFIX", raising=False)
    monkeypatch.setattr(sh, "SCRIPTS", scripts)
    monkeypatch.setattr(sh, "process_commandlines", lambda runner=None: [])
    monkeypatch.setattr(sh, "schtasks_rows", lambda name=None, runner=None: [])
    return logs


def kinds(found):
    return [f[0] for f in found]


# ---------- pure rules ----------
def test_nothing_wrong_means_nothing_raised():
    assert sh.problems(set(), [("Assistant A", "Ready", "0")], [], []) == []


def test_a_missing_daemon_is_the_worst_finding():
    found = sh.problems(set(), [], [], [], daemons=(DAEMON,))
    assert len(found) == 1
    kind, what, value, dedupe = found[0]
    assert kind == "daemon_down" and value == 5 and DAEMON in what and dedupe == "daemon-down-" + DAEMON
    assert sh.problems({DAEMON}, [], [], [], daemons=(DAEMON,)) == []


def test_benign_task_results_are_ignored_and_real_failures_are_not():
    tasks = [("A", "Ready", "0"), ("B", "Ready", "267011"), ("C", "Ready", "267009"), ("D", "Ready", "267014"), ("E", "Ready", ""), ("Bad", "Ready", "2")]
    found = sh.problems(set(), tasks, [], [])
    assert kinds(found) == ["task_failing"] and found[0][1] == "scheduled task Bad last exited 2" and found[0][2] == 3


def test_a_disabled_task_is_a_low_value_finding_but_a_failing_one_is_reported_as_failing():
    assert [(f[0], f[2]) for f in sh.problems(set(), [("Off", "Disabled", "0")], [], [])] == [("task_disabled", 2)]
    assert kinds(sh.problems(set(), [("Off", "Disabled", "2")], [], [])) == ["task_failing"]


def test_stale_logs_and_kill_switches_are_reported_worst_first():
    found = sh.problems(set(), [("Bad", "Ready", "2")], ["keep_going_off.txt"], [("x.log", 300, "nothing written")], daemons=(DAEMON,))
    assert [f[2] for f in found] == [5, 4, 3, 2]
    assert found[1][1] == "x.log has not changed for 300 min: nothing written"
    assert found[3][3] == "switch-keep_going_off.txt"


def test_many_findings_of_one_kind_become_one_grouped_work_item():
    tasks = [("T%d" % n, "Ready", "2") for n in range(5)]
    grouped = sh.group(sh.problems(set(), tasks, [], []))
    assert len(grouped) == 1
    kind, what, value, dedupe = grouped[0]
    assert kind == "task_failing" and what.startswith("5 task failing findings") and dedupe == "task_failing-group-5" and "T0" in what and "T4" in what


def test_two_findings_of_a_kind_stay_separate_and_other_kinds_survive_grouping():
    assert len(sh.group(sh.problems(set(), [("A", "Ready", "2"), ("B", "Ready", "2")], [], []))) == 2
    tasks = [("T%d" % n, "Ready", "2") for n in range(3)]
    grouped = sh.group(sh.problems(set(), tasks, [], [], daemons=(DAEMON,)))
    assert [g[0] for g in grouped] == ["daemon_down", "task_failing"]


def test_known_issues_are_dropped_until_their_date_and_only_by_exact_name():
    found = sh.problems(set(), [("Nightly Check", "Ready", "1"), ("Other", "Ready", "2")], [], [])
    known = {"Nightly Check": {"reason": "waiting for a password", "until": "2026-11-06"}}
    assert [f[1] for f in sh.drop_known(found, known, "2026-10-30")] == ["scheduled task Other last exited 2"]
    assert len(sh.drop_known(found, known, "2026-11-07")) == 2                                  # expired: it comes back
    assert len(sh.drop_known(found, {"Nightly": {"until": "2099-01-01"}}, "2026-10-30")) == 2    # a partial name does not match
    assert sh.drop_known(found, {}, "2026-10-30") == found and sh.drop_known(found, None, "2026-10-30") == found
    assert sh.drop_known(found, {"Nightly Check": "not a dict"}, "2026-10-30") == found          # a malformed entry hides nothing


def test_known_stale_logs_and_switches_are_dropped_too():
    found = sh.problems(set(), [], ["status_off.txt", "autopilot_off.txt"], [("feed.log", 300, "stopped")])
    kept = sh.drop_known(found, {"feed.log": {"until": "2027-01-01"}, "status_off.txt": {"until": "2027-01-01"}}, "2026-10-30")
    assert [f[3] for f in kept] == ["switch-autopilot_off.txt"]


def test_job_limit_is_three_missed_intervals_or_a_day_and_a_half_or_eight_days():
    assert sh.job_limit_min({"every_min": 5}) == 30 and sh.job_limit_min({"every_min": 120}) == 360
    assert sh.job_limit_min({"daily": "04:30"}) == 36 * 60 and sh.job_limit_min({"weekly": {"day": 6, "at": "04:00"}}) == 8 * 1440
    assert sh.job_limit_min({"every_min": "60m"}) == 36 * 60


def test_a_failing_stale_or_silent_job_is_found_and_a_healthy_one_is_not():
    jobs = [{"id": "a", "argv": ["a.py"], "every_min": 60}, {"id": "b", "argv": ["b.py"], "daily": "04:00"}, {"id": "c", "argv": ["c.py"], "every_min": 60},
            {"id": "off", "argv": ["o.py"], "every_min": 60, "enabled": False}]
    state = {"a": {"last_ok": NOW - 600, "fails": 0}, "b": {"last_ok": NOW - 40 * 3600, "fails": 0}, "c": {"last_ok": NOW - 100, "fails": 2, "last_code": 3}, "off": {"fails": 9}}
    found = sh.job_problems(jobs, state, NOW - 60, NOW)
    assert sorted(f[3] for f in found) == ["job-fail-c", "job-stale-b"]


def test_a_runner_that_stopped_ticking_is_the_worst_finding_and_no_jobs_means_no_finding():
    jobs = [{"id": "a", "argv": ["a.py"], "every_min": 60}]
    found = sh.job_problems(jobs, {}, NOW - 3600, NOW)
    assert found[0][0] == "job_runner_down" and found[0][2] == 5 and "install_scheduler.py --status" in found[0][1]
    assert sh.job_problems(jobs, {}, None, NOW)[0][0] == "job_runner_down"
    assert sh.job_problems([], {}, None, NOW) == [] and sh.job_problems(None, None, None, NOW) == []


def test_an_unparseable_table_is_a_value_five_finding():
    assert [(f[0], f[2]) for f in sh.job_problems([], {}, 1.0, NOW, table_broken=True)] == [("job_table_broken", 5)]


def test_the_runners_error_file_and_rows_without_a_schedule_become_findings_once():
    jobs = [{"id": "typo", "argv": ["a.py"], "every_mins": 5}, {"id": "fine", "argv": ["b.py"], "every_min": 60}]
    errs = [{"id": "badtime", "error": "ValueError: bad time"}, {"id": "typo", "error": "no recognised schedule key"}, "junk"]
    found = sh.job_problems(jobs, {"fine": {"last_ok": NOW - 60, "fails": 0}}, NOW - 30, NOW, errors=errs)
    assert sorted(f[3] for f in found) == ["job-error-badtime", "job-noschedule-typo"]


def test_a_row_that_never_ran_alarms_only_after_it_has_been_in_the_table_longer_than_its_limit():
    jobs = [{"id": "new", "argv": ["a.py"], "every_min": 60}]
    assert sh.job_problems(jobs, {}, NOW - 30, NOW, table_mtime=NOW - 600) == []
    late = sh.job_problems(jobs, {}, NOW - 30, NOW, table_mtime=NOW - 4 * 3600)
    assert [f[3] for f in late] == ["job-stale-new"] and "never run" in late[0][1]
    assert sh.job_problems(jobs, {"new": {"last_ok": NOW - 60, "fails": 0}}, NOW - 30, NOW, table_mtime=NOW - 4 * 3600) == []


def test_one_failed_run_is_enough_for_a_daily_or_weekly_job_but_not_for_a_frequent_one():
    jobs = [{"id": "d", "argv": ["d.py"], "daily": "04:30"}, {"id": "w", "argv": ["w.py"], "weekly": {"day": 6, "at": "04:00"}}, {"id": "f", "argv": ["f.py"], "every_min": 5}]
    state = {k: {"last_ok": NOW - 100, "fails": 1, "last_code": 1} for k in "dwf"}
    assert sorted(f[3] for f in sh.job_problems(jobs, state, NOW - 60, NOW)) == ["job-fail-d", "job-fail-w"]


def test_a_malformed_state_value_never_crashes_the_sensor():
    jobs = [{"id": "bad", "argv": ["a.py"], "every_min": "60m"}]
    state = {"bad": {"last_ok": "never", "fails": "lots"}}
    assert sh.job_problems(jobs, state, NOW - 30, NOW) == []


# ---------- process and task readers ----------
def test_daemons_running_finds_names_in_command_lines_and_assumes_fine_when_blind():
    lines = ["/usr/bin/python3 /home/x/Listener.py --port 1", "bash"]
    assert sh.daemons_running(["listener.py", "other.py"], lines) == {"listener.py"}
    assert sh.daemons_running(["listener.py"], []) == set()
    assert sh.daemons_running(["listener.py"], None) == {"listener.py"}


SCHTASKS = (
    '"HOST","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result","Author","Task To Run","Start In","Comment","Scheduled Task State"\n'
    '"HOST","\\Assistant JobRunner - demo","N/A","Ready","Interactive only","1/1/2027 1:00:00 AM","0","me","python x","N/A","N/A","Enabled"\n'
    '"HOST","","N/A","Ready","Interactive only","N/A","0","me","python x","N/A","N/A","Enabled"\n'
    '"HOST","\\Assistant Old","N/A","Disabled","Interactive only","1/1/2027 1:00:00 AM","267011","me","python y","N/A","N/A","Disabled"\n'
    '"HOST","\\Other Vendor Task","N/A","Ready","Interactive only","1/1/2027 1:00:00 AM","-2147024894","me","z","N/A","N/A","Enabled"\n'
    '"short","row"\n')


def test_parse_schtasks_reads_by_position_skips_headers_extra_triggers_and_short_rows():
    rows = sh.parse_schtasks(SCHTASKS)
    assert [r["name"] for r in rows] == ["Assistant JobRunner - demo", "Assistant Old", "Other Vendor Task"]
    assert rows[1]["state"] == "Disabled" and rows[2]["last_result"] == "-2147024894"
    assert sh.parse_schtasks("") == [] and sh.parse_schtasks(None) == []


def test_schtasks_rows_is_empty_off_windows_and_never_starts_anything(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert sh.schtasks_rows(runner=lambda argv: (_ for _ in ()).throw(AssertionError("must not run"))) == []


def test_schtasks_rows_on_windows_uses_the_query_and_a_single_task_name(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    seen = []
    rows = sh.schtasks_rows(name="Assistant Old", runner=lambda argv: (seen.append(argv) or (0, SCHTASKS)))
    assert seen[0][:2] == ["schtasks", "/Query"] and seen[0][-2:] == ["/TN", "Assistant Old"] and len(rows) == 3
    assert sh.schtasks_rows(runner=lambda argv: (1, "")) == []


def test_tasks_are_filtered_by_prefix_and_an_empty_prefix_matches_nothing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    got = sh._tasks("assistant", runner=lambda argv: (0, SCHTASKS))
    assert [g[0] for g in got] == ["Assistant JobRunner - demo", "Assistant Old"]
    assert sh._tasks("", runner=lambda argv: (0, SCHTASKS)) == []


def test_process_commandlines_uses_ps_off_windows_and_powershell_on_windows(monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "platform", "linux")
    assert sh.process_commandlines(runner=lambda a: (seen.append(a) or (0, "bash\npython3 listener.py\n"))) == ["bash", "python3 listener.py"]
    assert seen[0][0] == "ps"
    monkeypatch.setattr(sys, "platform", "win32")
    sh.process_commandlines(runner=lambda a: (seen.append(a) or (0, "x")))
    assert seen[1][0] == "powershell"
    assert sh.process_commandlines(runner=lambda a: (127, "")) is None


def test_the_real_runner_never_raises_for_a_missing_program():
    code, out = sh._run(["definitely-not-a-program-xyz"])
    assert code == 127 and out == ""


# ---------- config and files ----------
def test_config_defaults_and_a_bad_shape_is_ignored_piece_by_piece(box):
    assert sh.load_config(box) == {"daemons": [], "fresh": list(sh.DEFAULT_FRESH), "task_prefix": ""}
    (box / "sense_health.json").write_text(json.dumps({
        "daemons": ["listener.py", 5, "", "x" * 200], "task_prefix": "Mine",
        "fresh": [{"file": "a.log", "minutes": 30, "why": "stopped"}, {"file": "../etc/passwd"}, {"file": "b.log", "minutes": "soon"}, "junk"]}), encoding="utf-8")
    cfg = sh.load_config(box)
    assert cfg["daemons"] == ["listener.py"] and cfg["task_prefix"] == "Mine"
    assert cfg["fresh"][-1] == ("a.log", 30, "stopped") and len(cfg["fresh"]) == len(sh.DEFAULT_FRESH) + 1
    (box / "sense_health.json").write_text("[not json", encoding="utf-8")
    assert sh.load_config(box)["daemons"] == []


def test_task_prefix_comes_from_the_environment_then_the_config_then_nothing(monkeypatch):
    assert sh.task_prefix({}) == ""                         # no prefix: the task check is off and nothing is queried
    assert sh.task_prefix({"task_prefix": "Mine"}) == "Mine"
    monkeypatch.setenv("HARNESS_TASK_PREFIX", "FromEnv")
    assert sh.task_prefix({"task_prefix": "Mine"}) == "FromEnv"


def test_switches_are_every_off_file_plus_the_halt_file_but_not_this_sensors_own(box):
    for n in ("autopilot_off.txt", "send_gate_off.txt", "sense_health_off.txt", "loop_halt.txt", "notes.txt"):
        (box / n).write_text("x", encoding="utf-8")
    assert sh._switches_on(box) == ["autopilot_off.txt", "loop_halt.txt", "send_gate_off.txt"]


def test_stale_logs_are_reported_and_a_missing_log_is_not(box):
    old = box / "old.log"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (NOW - 7200, NOW - 7200))
    fresh = box / "new.log"
    fresh.write_text("x", encoding="utf-8")
    os.utime(fresh, (NOW - 60, NOW - 60))
    got = sh._stale(box, [("old.log", 90, "stopped"), ("new.log", 90, "stopped"), ("missing.log", 90, "stopped")], NOW)
    assert got == [("old.log", 120, "stopped")]


def test_job_findings_for_a_missing_broken_and_healthy_table(box, tmp_path):
    table = tmp_path / "jt.json"
    assert sh._jobs_findings(box, NOW, table) == []                                       # no table: nothing scheduled
    table.write_text("[{ nope", encoding="utf-8")
    assert [f[0] for f in sh._jobs_findings(box, NOW, table)] == ["job_table_broken"]
    table.write_text('{"not": "a list"}', encoding="utf-8")
    assert [f[0] for f in sh._jobs_findings(box, NOW, table)] == ["job_table_broken"]
    table.write_text(json.dumps([{"id": "a", "argv": ["a.py"], "every_min": 60}]), encoding="utf-8")
    (box / "heartbeats").mkdir()
    (box / "heartbeats" / "job_runner.beat").write_text(str(time.time()), encoding="utf-8")
    (box / "job_runner_state.json").write_text(json.dumps({"a": {"last_ok": time.time(), "fails": 0}}), encoding="utf-8")
    assert sh._jobs_findings(box, time.time(), table) == []                               # ticking, healthy
    (box / "heartbeats" / "job_runner.beat").write_text("garbage", encoding="utf-8")
    assert [f[0] for f in sh._jobs_findings(box, time.time(), table)] == ["job_runner_down"]


# ---------- the whole run ----------
def test_dry_run_writes_nothing(box, monkeypatch, capsys):
    (box / "sense_health.json").write_text(json.dumps({"daemons": [DAEMON]}), encoding="utf-8")
    (box / "autopilot_off.txt").write_text("x", encoding="utf-8")
    before = sorted(os.listdir(box))
    assert sh.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would raise v5 daemon_down" in out and "2 finding(s), nothing written" in out
    assert sorted(os.listdir(box)) == before


def test_main_writes_one_event_and_one_work_item_per_new_problem_and_dedupes(box, monkeypatch, capsys):
    (box / "sense_health.json").write_text(json.dumps({"daemons": [DAEMON]}), encoding="utf-8")
    (box / "autopilot_off.txt").write_text("x", encoding="utf-8")
    assert sh.main([]) == 0
    assert "2 finding(s), 2 new intent(s)" in capsys.readouterr().out
    intents = intent_store.read_jsonl(intent_store.INTENTS, str(box))
    down = [i for i in intents if i["kind"] == "daemon_down"][0]
    sw = [i for i in intents if i["kind"] == "switch_on"][0]
    assert down["tier"] == 1 and down["value"] == 5 and sw["tier"] == 2          # a dead daemon is the assistant's to fix, a switch left on is the user's call
    assert len(intent_store.read_jsonl(intent_store.EVENTS, str(box))) == 2
    alerts = (box / "alerts.txt").read_text(encoding="utf-8")
    assert alerts.count("sense_health:") == 2
    assert sh.main([]) == 0
    assert "2 finding(s), 0 new intent(s)" in capsys.readouterr().out             # same problems, nothing new
    assert len(intent_store.read_jsonl(intent_store.INTENTS, str(box))) == 2
    assert (box / "alerts.txt").read_text(encoding="utf-8") == alerts            # and no second alert line either


def test_kill_switch_stops_it(box, capsys):
    (box / "sense_health_off.txt").write_text("off", encoding="utf-8")
    (box / "sense_health.json").write_text(json.dumps({"daemons": [DAEMON]}), encoding="utf-8")
    assert sh.main([]) == 0 and "off" in capsys.readouterr().out
    assert not (box / "intents.jsonl").exists()


def test_a_crash_in_the_job_table_check_does_not_stop_the_other_checks(box, monkeypatch, capsys):
    (box / "sense_health.json").write_text(json.dumps({"daemons": [DAEMON]}), encoding="utf-8")

    def boom(*a, **k):
        raise ValueError("could not convert string to float")
    monkeypatch.setattr(sh, "_jobs_findings", boom)
    assert sh.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "%s is not running" % DAEMON in out and "job-table check itself failed" in out


def test_known_issues_in_the_logs_folder_silence_a_finding(box, capsys):
    (box / "autopilot_off.txt").write_text("x", encoding="utf-8")
    (box / "known_issues.json").write_text(json.dumps({"autopilot_off.txt": {"reason": "on purpose", "until": "2999-01-01"}}), encoding="utf-8")
    assert sh.main(["--dry-run"]) == 0
    assert "0 finding(s)" in capsys.readouterr().out


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "loop_halt.txt").write_text("x", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "sense_health.py"), "--dry-run"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "would raise v2 switch_on" in r.stdout and "loop_halt.txt" in r.stdout
    assert not (root / "logs" / "intents.jsonl").exists()
