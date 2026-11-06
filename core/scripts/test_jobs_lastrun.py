# -*- coding: utf-8 -*-
"""Tests for jobs_lastrun.py"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jobs_lastrun


CSV = (
    '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result","Author","Task To Run","Start In","Comment","Scheduled Task State"\n'
    '"PC","\\Assistant JobRunner - kit","N/A","Ready","Interactive","11/5/2026 10:00:00 AM","0","Me","C:\\kit\\job_runner.py","N/A","N/A","Enabled"\n'
    '"PC","\\Assistant Backup","N/A","Ready","Interactive","11/5/2026 3:00:00 AM","267011","Me","C:\\kit\\backup.py","N/A","N/A","Enabled"\n'
    '"PC","\\Assistant Broken Thing","N/A","Ready","Interactive","11/5/2026 2:00:00 AM","1","Me","C:\\kit\\broken.py","N/A","N/A","Enabled"\n'
    '"PC","\\Microsoft\\Windows\\Defrag\\ScheduledDefrag","N/A","Ready","Interactive","N/A","0","Microsoft Corporation","defrag.exe","N/A","N/A","Enabled"\n'
    '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result","Author","Task To Run","Start In","Comment","Scheduled Task State"\n'
    '"PC","\\Assistant JobRunner - kit","N/A","Ready","Interactive","11/5/2026 10:00:00 AM","0","Me","C:\\kit\\job_runner.py","N/A","N/A","Enabled"\n'
)


def parse_csv(text):
    import csv
    import io
    return list(csv.DictReader(io.StringIO(text)))


# ---------- default_filter ----------
class TestDefaultFilter:
    def test_default_is_assistant(self, monkeypatch):
        monkeypatch.delenv("HARNESS_TASK_NAME", raising=False)
        assert jobs_lastrun.default_filter() == "assistant"

    def test_env_override_is_lower_cased(self, monkeypatch):
        monkeypatch.setenv("HARNESS_TASK_NAME", "MyTasks")
        assert jobs_lastrun.default_filter() == "mytasks"

    def test_blank_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HARNESS_TASK_NAME", "   ")
        assert jobs_lastrun.default_filter() == "assistant"


# ---------- is_ok ----------
class TestIsOk:
    @pytest.mark.parametrize("code", ["0", "267009", "267011"])
    def test_ok_codes(self, code):
        assert jobs_lastrun.is_ok(code) is True

    @pytest.mark.parametrize("code", ["1", "2", "267014", "garbage"])
    def test_bad_codes(self, code):
        assert jobs_lastrun.is_ok(code) is False

    def test_none_is_not_ok(self):
        assert jobs_lastrun.is_ok(None) is False

    def test_padding_is_stripped(self):
        assert jobs_lastrun.is_ok("  0  ") is True


# ---------- live_known_issues ----------
class TestLiveKnownIssues:
    def test_future_until_is_live(self):
        data = {"\\Assistant Broken Thing": {"reason": "waiting on a credential", "until": "2099-01-01"}}
        assert jobs_lastrun.live_known_issues(data, today="2026-11-06") == {"\\Assistant Broken Thing": "waiting on a credential"}

    def test_past_until_is_dropped(self):
        data = {"\\Assistant Broken Thing": {"reason": "stale", "until": "2020-01-01"}}
        assert jobs_lastrun.live_known_issues(data, today="2026-11-06") == {}

    def test_today_equals_until_is_live(self):
        data = {"\\X": {"reason": "r", "until": "2026-11-06"}}
        assert jobs_lastrun.live_known_issues(data, today="2026-11-06") == {"\\X": "r"}

    def test_non_dict_entry_is_dropped(self):
        data = {"\\X": "not a dict"}
        assert jobs_lastrun.live_known_issues(data, today="2026-11-06") == {}

    def test_missing_until_is_dropped(self):
        data = {"\\X": {"reason": "no expiry set"}}
        assert jobs_lastrun.live_known_issues(data, today="2026-11-06") == {}

    def test_empty_or_none_input(self):
        assert jobs_lastrun.live_known_issues({}) == {}
        assert jobs_lastrun.live_known_issues(None) == {}


# ---------- classify_rows ----------
class TestClassifyRows:
    def test_filters_by_name_and_dedupes_repeated_header_blocks(self):
        rows = parse_csv(CSV)
        out = jobs_lastrun.classify_rows(rows, "assistant", {}, set())
        names = [r[1] for r in out]
        assert names == ["\\Assistant JobRunner - kit", "\\Assistant Backup", "\\Assistant Broken Thing"]

    def test_non_matching_name_excluded(self):
        rows = parse_csv(CSV)
        out = jobs_lastrun.classify_rows(rows, "assistant", {}, set())
        assert all("Defrag" not in r[1] for r in out)

    def test_ok_code_labeled_ok(self):
        rows = parse_csv(CSV)
        out = jobs_lastrun.classify_rows(rows, "assistant", {}, set())
        by_name = {r[1]: r[0] for r in out}
        assert by_name["\\Assistant JobRunner - kit"] == "OK  "

    def test_never_run_is_ok_not_fail(self):
        rows = parse_csv(CSV)
        out = jobs_lastrun.classify_rows(rows, "assistant", {}, set())
        by_name = {r[1]: r[0] for r in out}
        assert by_name["\\Assistant Backup"] == "OK  "

    def test_real_failure_labeled_fail(self):
        rows = parse_csv(CSV)
        out = jobs_lastrun.classify_rows(rows, "assistant", {}, set())
        by_name = {r[1]: r[0] for r in out}
        assert by_name["\\Assistant Broken Thing"] == "FAIL"

    def test_known_issue_suppresses_fail_label(self):
        rows = parse_csv(CSV)
        known = {"\\Assistant Broken Thing": "waiting on a credential"}
        out = jobs_lastrun.classify_rows(rows, "assistant", known, set())
        by_name = {r[1]: r[0] for r in out}
        assert by_name["\\Assistant Broken Thing"] == "KNOWN"

    def test_self_excluded_takes_priority_over_fail(self):
        rows = parse_csv(CSV)
        out = jobs_lastrun.classify_rows(rows, "assistant", {}, {"\\Assistant Broken Thing"})
        by_name = {r[1]: r[0] for r in out}
        assert by_name["\\Assistant Broken Thing"] == "SELF"

    def test_ok_wins_over_known_and_self(self):
        """A task that is both listed as known and currently passing is still shown as OK, not KNOWN."""
        rows = parse_csv(CSV)
        known = {"\\Assistant JobRunner - kit": "irrelevant"}
        out = jobs_lastrun.classify_rows(rows, "assistant", known, {"\\Assistant JobRunner - kit"})
        by_name = {r[1]: r[0] for r in out}
        assert by_name["\\Assistant JobRunner - kit"] == "OK  "

    def test_empty_rows(self):
        assert jobs_lastrun.classify_rows([], "assistant", {}, set()) == []


# ---------- self_exclude_names ----------
class TestSelfExcludeNames:
    def test_empty_env_gives_empty_set(self, monkeypatch):
        monkeypatch.delenv("HARNESS_SELFTEST_TASKS", raising=False)
        assert jobs_lastrun.self_exclude_names() == set()

    def test_comma_separated_names_are_split_and_trimmed(self, monkeypatch):
        monkeypatch.setenv("HARNESS_SELFTEST_TASKS", r"\Assistant SelfTest, \Other Task ")
        assert jobs_lastrun.self_exclude_names() == {"\\Assistant SelfTest", "\\Other Task"}

    def test_blank_entries_are_dropped(self, monkeypatch):
        monkeypatch.setenv("HARNESS_SELFTEST_TASKS", r"\A,, ,\B")
        assert jobs_lastrun.self_exclude_names() == {"\\A", "\\B"}


# ---------- _read_known_issues ----------
class TestReadKnownIssues:
    def test_missing_file_gives_empty_dict(self, tmp_path):
        assert jobs_lastrun._read_known_issues(str(tmp_path)) == {}

    def test_reads_live_entries(self, tmp_path):
        data = {"\\Assistant Broken Thing": {"reason": "known", "until": "2099-01-01"}}
        (tmp_path / "known_issues.json").write_text(json.dumps(data), encoding="utf-8")
        got = jobs_lastrun._read_known_issues(str(tmp_path))
        assert got == {"\\Assistant Broken Thing": "known"}

    def test_malformed_json_gives_empty_dict(self, tmp_path):
        (tmp_path / "known_issues.json").write_text("not json", encoding="utf-8")
        assert jobs_lastrun._read_known_issues(str(tmp_path)) == {}

    def test_json_that_is_not_an_object_gives_empty_dict(self, tmp_path):
        (tmp_path / "known_issues.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert jobs_lastrun._read_known_issues(str(tmp_path)) == {}


# ---------- main (CLI) ----------
class TestMain:
    def test_off_windows_prints_and_returns_zero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "linux")
        rc = jobs_lastrun.main([])
        assert rc == 0
        assert "Windows only" in capsys.readouterr().out

    def test_schtasks_query_failure_returns_one(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(jobs_lastrun, "_query_schtasks", lambda timeout=60: (1, ""))
        rc = jobs_lastrun.main([])
        assert rc == 1
        assert "could not query" in capsys.readouterr().out

    def test_all_ok_returns_zero(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(jobs_lastrun, "LOGS", str(tmp_path))
        monkeypatch.delenv("HARNESS_TASK_NAME", raising=False)
        monkeypatch.delenv("HARNESS_SELFTEST_TASKS", raising=False)
        ok_csv = (
            '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result","Author","Task To Run","Start In","Comment","Scheduled Task State"\n'
            '"PC","\\Assistant JobRunner - kit","N/A","Ready","Interactive","N/A","0","Me","C:\\kit\\job_runner.py","N/A","N/A","Enabled"\n'
        )
        monkeypatch.setattr(jobs_lastrun, "_query_schtasks", lambda timeout=60: (0, ok_csv))
        rc = jobs_lastrun.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "OK" in out
        assert "1/1 assistant tasks healthy" in out

    def test_a_real_failure_returns_one(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(jobs_lastrun, "LOGS", str(tmp_path))
        monkeypatch.delenv("HARNESS_TASK_NAME", raising=False)
        monkeypatch.delenv("HARNESS_SELFTEST_TASKS", raising=False)
        monkeypatch.setattr(jobs_lastrun, "_query_schtasks", lambda timeout=60: (0, CSV))
        rc = jobs_lastrun.main([])
        assert rc == 1
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "\\Assistant Broken Thing" in out

    def test_known_issue_avoids_nonzero_exit(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(jobs_lastrun, "LOGS", str(tmp_path))
        monkeypatch.delenv("HARNESS_TASK_NAME", raising=False)
        monkeypatch.delenv("HARNESS_SELFTEST_TASKS", raising=False)
        known = {"\\Assistant Broken Thing": {"reason": "waiting on a credential", "until": "2099-01-01"}}
        (tmp_path / "known_issues.json").write_text(json.dumps(known), encoding="utf-8")
        monkeypatch.setattr(jobs_lastrun, "_query_schtasks", lambda timeout=60: (0, CSV))
        rc = jobs_lastrun.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "KNOWN" in out
        assert "1 known issue(s)" in out

    def test_self_excluded_task_avoids_nonzero_exit(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(jobs_lastrun, "LOGS", str(tmp_path))
        monkeypatch.delenv("HARNESS_TASK_NAME", raising=False)
        monkeypatch.setenv("HARNESS_SELFTEST_TASKS", "\\Assistant Broken Thing")
        monkeypatch.setattr(jobs_lastrun, "_query_schtasks", lambda timeout=60: (0, CSV))
        rc = jobs_lastrun.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "SELF" in out

    def test_custom_task_name_filter_via_env(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(jobs_lastrun, "LOGS", str(tmp_path))
        monkeypatch.setenv("HARNESS_TASK_NAME", "Defrag")
        monkeypatch.delenv("HARNESS_SELFTEST_TASKS", raising=False)
        monkeypatch.setattr(jobs_lastrun, "_query_schtasks", lambda timeout=60: (0, CSV))
        rc = jobs_lastrun.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ScheduledDefrag" in out
        assert "Assistant" not in out
        assert "1/1 defrag tasks healthy" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
