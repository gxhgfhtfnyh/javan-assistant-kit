# -*- coding: utf-8 -*-
"""Tests for lab_report.py: the pure summaries, each section on temporary logs, the note and its index line, the switches and a real process."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lab_report as lr  # noqa: E402

NOW = time.mktime((2026, 10, 31, 12, 0, 0, 0, 0, -1))


def stamp(offset_s):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - offset_s))


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    scripts = root / "scripts"
    for d in (scripts, root / "logs", root / "vault" / "05 - Resources"):
        d.mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    monkeypatch.setattr(lr, "_scripts", lambda: scripts)
    return root


def put(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")


def put_jsonl(path, rows):
    put(path, "".join(json.dumps(r) + "\n" for r in rows))


def test_alert_lines_parse_with_and_without_brackets_and_sources():
    t, src = lr.parse_alert_line("[%s] persistence_audit: a new program" % stamp(60))
    assert src == "persistence_audit" and abs(t - (NOW - 60)) < 1
    assert lr.parse_alert_line("%s INTEGRITY CHANGE: x" % stamp(0))[1] == "other"
    assert lr.parse_alert_line("%s stop_gate hourly cap reached" % stamp(0))[1] == "other"
    assert lr.parse_alert_line("not an alert") == (None, None) and lr.parse_alert_line("") == (None, None)


def test_alert_summary_counts_the_last_day_by_source():
    lines = ["[%s] a: x" % stamp(100), "[%s] a: y" % stamp(200), "[%s] b: z" % stamp(300), "[%s] a: old" % stamp(3 * 86400), "junk"]
    assert lr.summarize_alerts(lines, NOW) == {"total": 3, "by_source": {"a": 2, "b": 1}}
    assert lr.summarize_alerts([], NOW) == {"total": 0, "by_source": {}}


def test_work_list_summary_counts_open_items_by_tier_and_the_oldest():
    intents = [{"status": "open", "tier": 1, "epoch": NOW - 2 * 86400}, {"status": "doing", "tier": 0, "epoch": NOW - 3600}, {"status": "waiting", "tier": 2},
               {"status": "done", "tier": 1}, {"status": "dropped", "tier": 1}, "junk"]
    s = lr.summarize_intents(intents, NOW)
    assert s == {"open": 3, "by_tier": {0: 1, 1: 1, 2: 1}, "oldest_days": 2.0, "dropped": 1}
    assert lr.summarize_intents([], NOW) == {"open": 0, "by_tier": {}, "oldest_days": None, "dropped": 0}


def test_outcomes_in_the_window_are_counted_by_result():
    rows = [{"ts": stamp(100), "result": "ok"}, {"ts": stamp(200), "result": "ok"}, {"ts": stamp(300), "result": "fail"}, {"ts": stamp(5 * 86400), "result": "ok"}, {"ts": "bad"}]
    assert lr.summarize_outcomes(rows, NOW) == {"ok": 2, "fail": 1}


def test_claim_counts_in_the_window():
    recs = [{"ts": stamp(100), "claims": ["fixed"], "flagged_no_tools": True, "flagged_errors": False, "offers": 5, "gaps": ["write"]},
            {"ts": stamp(200), "claims": ["done"], "flagged_no_tools": False, "flagged_errors": True, "offers": 0},
            {"ts": stamp(300), "claims": [], "offers": 2, "refusals": ["I can't do that"]},
            {"ts": stamp(30 * 86400), "claims": ["saved"], "flagged_no_tools": True, "offers": 9}]
    assert lr.summarize_claims(recs, NOW) == {"claim_replies": 2, "flagged_no_tools": 1, "flagged_errors": 1, "gaps": 1, "refusals": 1, "offers": 7}
    assert lr.summarize_claims(recs)["claim_replies"] == 3                       # no window: everything
    assert lr.summarize_claims([], NOW) == {"claim_replies": 0, "flagged_no_tools": 0, "flagged_errors": 0, "gaps": 0, "refusals": 0, "offers": 0}


def test_recall_summary_gives_the_latest_score_and_the_change():
    rows = [{"total": 20, "passed": 18, "timestamp": "2026-10-30T10:00:00Z"}, {"total": 20, "passed": 19, "timestamp": "2026-10-31T10:00:00Z"}, {"total": 0, "passed": 0}, {"junk": 1}]
    t = lr.summarize_recall(rows)
    assert t.startswith("19/20 (95%) at 2026-10-31T10:00") and "+5 points" in t
    assert lr.summarize_recall([{"total": 4, "passed": 2}]) == "2/4 (50%) at ?" and lr.summarize_recall([]) is None


def test_health_summary_names_the_red_invariants():
    state = {"green": 6, "measured": 8, "ts": "2026-10-31T11:00:00", "results": {"A1": ["red", "work waiting with no session"], "A2": ["green", "ok"], "A3": ["red", "dropped"]}}
    t = lr.summarize_health(state)
    assert t.startswith("6 of 8 measured invariants green; red: A1 (work waiting with no session); A3 (dropped)")
    assert lr.summarize_health({}) is None and lr.summarize_health(None) is None and "; red:" not in lr.summarize_health({"green": 2, "measured": 2, "results": {}})


def test_job_summary_lists_failing_and_never_run_enabled_jobs():
    jobs = [{"id": "a"}, {"id": "b", "enabled": False}, {"id": "c"}, {"id": "d"}, {"bad": 1}]
    state = {"a": {"last_run": 5, "fails": 2, "last_code": 1}, "c": {"last_run": 5, "fails": 0}}
    assert lr.summarize_jobs(state, jobs) == {"jobs": 4, "failing": [("a", 2, 1)], "never_ran": ["d"]}
    assert lr.summarize_jobs(None, None) == {"jobs": 0, "failing": [], "never_ran": []}


def test_the_last_tally_line_is_found():
    assert lr.tally_from_text("noise\n41/43 passed\nmore\n43/43 passed\ntrailer") == "43/43 passed"
    assert lr.tally_from_text("===== 12 passed, 1 failed in 3s =====") == "===== 12 passed, 1 failed in 3s ====="
    assert lr.tally_from_text("nothing here") is None and lr.tally_from_text(None) is None


def test_an_empty_assistant_folder_still_gives_a_full_report_that_says_what_is_missing(env):
    text = lr.build(NOW, hygiene=False)
    for needle in ("# Lab report 2026-10-31", "## Alerts", "no alerts file yet", "no work items yet", "no claim gate log yet", "no recall eval yet",
                   "no harness health result yet", "no job table", "no self-test result yet"):
        assert needle in text, needle


def test_a_report_over_real_logs_shows_each_section_with_numbers(env):
    lg = env / "logs"
    put(lg / "alerts.txt", "[%s] hygiene_audit: flag one\n[%s] disk_space_check: low\n" % (stamp(60), stamp(120)))
    put_jsonl(lg / "intents.jsonl", [{"id": "1", "status": "open", "tier": 1, "epoch": NOW - 86400}])
    put_jsonl(lg / "outcomes.jsonl", [{"ts": stamp(60), "result": "ok"}])
    put_jsonl(lg / "claim_gate.jsonl", [{"ts": stamp(60), "claims": ["done"], "flagged_no_tools": True}])
    put_jsonl(lg / "memory_eval.jsonl", [{"total": 10, "passed": 9, "timestamp": "2026-10-31T09:00:00Z"}])
    put(lg / "harness_health.json", json.dumps({"green": 8, "measured": 8, "ts": "2026-10-31T11:50:00", "results": {}}))
    put(lg / "job_runner_state.json", json.dumps({"x": {"last_run": 1, "fails": 3, "last_code": 2}}))
    put(env / "scripts" / "jobs.json", json.dumps([{"id": "x"}, {"id": "y"}]))
    put(lg / "self_test.log", "run\n43/43 passed\n")
    text = lr.build(NOW, hygiene=False)
    for needle in ("alert lines in the last 24 hours: 2 (disk_space_check=1, hygiene_audit=1)", "open: 1 (tier1=1) | oldest open: 1.0 days", "closed in the last 24 hours: ok=1",
                   "replies with claim words: 1 | with zero tool calls: 1", "recall eval, latest: 9/10 (90%)", "8 of 8 measured invariants green", "failing: x (fails 3, last code 2)",
                   "enabled but never run: y", "self-test, last logged: 43/43 passed"):
        assert needle in text, needle


def test_corrupt_logs_do_not_break_the_report(env):
    lg = env / "logs"
    put(lg / "intents.jsonl", "{broken\n\x00\x01\n[1,2]\n")
    put(lg / "harness_health.json", "{not json")
    put(lg / "alerts.txt", "\xff\xfe garbage\n")
    put(env / "scripts" / "jobs.json", "{not a list")
    text = lr.build(NOW, hygiene=False)
    assert "# Lab report" in text and "no job table" in text


def test_the_hygiene_section_is_included_when_the_audit_script_is_present(env, monkeypatch):
    import hygiene_audit
    monkeypatch.setattr(hygiene_audit, "_scripts", lambda: env / "scripts")
    text = lr.build(NOW, hygiene=True)
    assert "## Hygiene (automatic)" in text and "- FLAGS: none" in text


def test_the_self_test_runs_only_when_asked_and_only_the_one_script(env):
    assert "does not exist" in lr.build(NOW, self_test=True, hygiene=False)
    put(env / "scripts" / "self_test.py", "print('FAIL one')\nprint('4/5 passed')\n")
    text = lr.build(NOW, self_test=True, hygiene=False)
    assert "self-test (run now): 4/5 passed; FAILED: FAIL one" in text
    assert "run now" not in lr.build(NOW, hygiene=False)


def test_write_note_makes_one_valid_note_that_is_overwritten_not_piled_up(env):
    p = lr.write_note("# Lab report\nbody", now=NOW)
    assert p == env / "vault" / "05 - Resources" / "Lab Report.md"
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---\nstatus: active\ntype: resource\ntopic: harness\nchecked: 2026-10-31\n---\n# Lab report")
    lr.write_note("# second", now=NOW)
    assert "second" in p.read_text(encoding="utf-8") and "body" not in p.read_text(encoding="utf-8")
    assert len(list((env / "vault" / "05 - Resources").iterdir())) == 1


def test_write_note_does_nothing_when_the_vault_folder_is_missing(env, tmp_path):
    assert lr.write_note("x", vault=tmp_path / "no-vault") is None
    assert not (tmp_path / "no-vault").exists()


def test_the_index_gets_one_line_once_in_a_generated_notes_section_before_the_rules(env):
    idx = env / "vault" / "VAULT-INDEX.md"
    put(idx, "# Vault Index\n\n## Folders\n- a\n\n## Rules for keeping this index true\n- be right\n")
    assert lr.ensure_indexed() is True
    text = idx.read_text(encoding="utf-8")
    assert text.index("## Generated notes") < text.index("[[Lab Report]]") < text.index("## Rules for keeping")
    assert lr.ensure_indexed() is False and idx.read_text(encoding="utf-8").count("[[Lab Report]]") == 1


def test_the_index_line_goes_into_an_existing_generated_notes_section_or_the_end(env):
    idx = env / "vault" / "VAULT-INDEX.md"
    put(idx, "# Index\n\n## Generated notes\n- [[Other]]\n")
    assert lr.ensure_indexed() is True
    lines = idx.read_text(encoding="utf-8").splitlines()
    assert lines[lines.index("## Generated notes") + 1].startswith("- [[Lab Report]]") and "- [[Other]]" in lines
    put(idx, "# Index\n\n- [[Other]]\n")
    assert lr.ensure_indexed() is True and idx.read_text(encoding="utf-8").rstrip().endswith("overwritten each run)")


def test_an_index_that_already_lists_the_note_with_an_alias_is_left_alone_and_a_missing_index_is_fine(env):
    idx = env / "vault" / "VAULT-INDEX.md"
    put(idx, "# Index\n- [[Lab Report|the report]]\n")
    assert lr.ensure_indexed() is False
    idx.unlink()
    assert lr.ensure_indexed() is False


def test_main_prints_writes_and_indexes_and_no_write_leaves_the_vault_alone(env, capsys):
    put(env / "vault" / "VAULT-INDEX.md", "# Index\n")
    assert lr.main(["--no-write"]) == 0
    assert not (env / "vault" / "05 - Resources" / "Lab Report.md").exists() and "# Lab report" in capsys.readouterr().out
    assert lr.main([]) == 0
    assert (env / "vault" / "05 - Resources" / "Lab Report.md").exists() and "[[Lab Report]]" in (env / "vault" / "VAULT-INDEX.md").read_text(encoding="utf-8")


def test_no_index_flag_and_a_missing_vault(env, capsys, tmp_path, monkeypatch):
    put(env / "vault" / "VAULT-INDEX.md", "# Index\n")
    assert lr.main(["--no-index"]) == 0
    assert (env / "vault" / "05 - Resources" / "Lab Report.md").exists() and "[[Lab Report]]" not in (env / "vault" / "VAULT-INDEX.md").read_text(encoding="utf-8")
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "gone"))
    assert lr.main([]) == 0 and "does not exist, so no note was written" in capsys.readouterr().out


def test_kill_switch_stops_the_job_and_bad_flags_exit_2(env, capsys):
    (env / "logs" / lr.OFF_NAME).write_text("off", encoding="utf-8")
    assert lr.main(["--job"]) == 0 and capsys.readouterr().out.strip() == "lab_report: off"
    assert not (env / "vault" / "05 - Resources" / "Lab Report.md").exists()
    assert lr.main(["--bogus"]) == 2


def test_a_write_failure_exits_1_by_hand_and_0_as_a_job(env, monkeypatch, capsys):
    monkeypatch.setattr(lr, "write_note", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
    assert lr.main([]) == 1 and lr.main(["--job"]) == 0
    assert "could not write the note (PermissionError)" in capsys.readouterr().out


def test_real_process_builds_the_report_for_its_own_environment(tmp_path):
    root = tmp_path / "r"
    (root / "logs").mkdir(parents=True)
    (root / "vault").mkdir()
    child_env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "lab_report.py"), "--job"], capture_output=True, text=True, env=child_env, timeout=60)
    assert r.returncode == 0 and "# Lab report" in r.stdout
    assert (root / "vault" / "05 - Resources" / "Lab Report.md").exists()
