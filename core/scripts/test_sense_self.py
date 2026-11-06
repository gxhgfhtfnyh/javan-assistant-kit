# -*- coding: utf-8 -*-
"""Tests for sense_self.py: the claim-gap and eval-regression rules, the log reader and the whole run on a temporary folder."""
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
import sense_self as se  # noqa: E402

NOW = time.mktime(time.strptime("2026-10-30 12:00:00", "%Y-%m-%d %H:%M:%S"))


@pytest.fixture
def logs(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(d))
    return d


def jwrite(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_claim_gaps_in_the_window_are_raised_and_old_or_clean_rows_are_not():
    rows = [{"ts": "2026-10-30 10:00:00", "gaps": ["write"], "text_head": "I saved the report"},
            {"ts": "2026-10-30 11:30:00", "gaps": [], "text_head": "fine"},
            {"ts": "2026-10-29 01:00:00", "gaps": ["tests"], "text_head": "old"}]
    got = se.claim_gaps(rows, NOW, hours=6)
    assert len(got) == 1 and "claimed write" in got[0][0] and got[0][1] == 4 and got[0][2] == "claimgap-2026-10-30 10:00:00"
    assert len(se.claim_gaps(rows, NOW, hours=40)) == 2


def test_claim_gaps_with_several_gaps_names_them_all():
    got = se.claim_gaps([{"ts": "2026-10-30 11:00:00", "gaps": ["write", "tests"], "text_head": "x" * 300}], NOW)
    assert "claimed write/tests" in got[0][0] and len(got[0][0]) < 200


def test_claim_gaps_survives_bad_rows():
    bad = [None, "x", {"ts": "bad", "gaps": ["write"]}, {"gaps": ["write"]}, {"ts": "2026-10-30 11:00:00", "gaps": "write"}, {"ts": 5, "gaps": ["write"]}]
    assert se.claim_gaps(bad, NOW) == [] and se.claim_gaps(None, NOW) == []


def test_eval_regression_needs_a_drop_of_two_against_the_best_with_the_same_total():
    rows = [{"timestamp": "a", "total": 20, "passed": 19}, {"timestamp": "b", "total": 20, "passed": 18}, {"timestamp": "c", "total": 20, "passed": 16}]
    got = se.eval_regressions(rows)
    assert len(got) == 1 and "fell to 16/20 from a best of 19/20" in got[0][0] and got[0][2] == "evalreg-recall---c"
    assert se.eval_regressions(rows[:2]) == []                                   # only one lower


def test_eval_regression_keeps_tools_and_modes_apart_and_never_compares_different_totals():
    a = {"tool": "/x/search.py", "mode": "search", "total": 20}
    b = {"tool": "/x/search.py", "mode": "stale", "total": 8}
    rows = [dict(a, ts="1", passed=19), dict(b, ts="2", passed=7), dict(a, ts="3", passed=19), dict(b, ts="4", passed=7)]
    assert se.eval_regressions(rows) == []
    assert se.eval_regressions([dict(a, ts="1", passed=19, total=10), dict(a, ts="2", passed=1)]) == []
    rows = [dict(a, ts="1", passed=19), dict(a, ts="2", passed=15)]
    got = se.eval_regressions(rows)
    assert "search.py (search)" in got[0][0] and got[0][2] == "evalreg-search.py-search-2"


def test_eval_regression_ignores_junk_rows_and_zero_totals():
    rows = [{"junk": 1}, None, "x", {"total": 0, "passed": 0}, {"total": True, "passed": 1}, {"total": 5, "passed": "many"}, {"total": 5}]
    assert se.eval_regressions(rows) == [] and se.eval_regressions(None) == []


def test_tail_jsonl_reads_the_end_skips_bad_lines_and_a_missing_file(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join(['{"n": 1}', "not json", "", '{"n": 2}', '{"n": 3}']) + "\n", encoding="utf-8")
    assert [r["n"] for r in se.tail_jsonl(p)] == [1, 2, 3]
    assert [r["n"] for r in se.tail_jsonl(p, rows=2)] == [2, 3]
    assert se.tail_jsonl(tmp_path / "missing.jsonl") == []


def test_tail_jsonl_reads_only_the_last_bytes_of_a_huge_log(tmp_path):
    p = tmp_path / "big.jsonl"
    p.write_text("".join('{"n": %d}\n' % i for i in range(5000)), encoding="utf-8")
    got = se.tail_jsonl(p, rows=3, cap=200)
    assert [r["n"] for r in got] == [4997, 4998, 4999]


def test_findings_combine_both_logs_and_tolerate_missing_ones(logs):
    assert se.findings(logs, NOW) == []
    jwrite(logs / "claim_gate.jsonl", [{"ts": "2026-10-30 11:00:00", "gaps": ["log"], "text_head": "logged it"}])
    jwrite(logs / "memory_eval.jsonl", [{"timestamp": "a", "total": 10, "passed": 10}, {"timestamp": "b", "total": 10, "passed": 7}])
    assert [f[2].split("-")[0] for f in se.findings(logs, NOW)] == ["claimgap", "evalreg"]


def test_dry_run_writes_nothing(logs, capsys):
    jwrite(logs / "memory_eval.jsonl", [{"timestamp": "a", "total": 10, "passed": 10}, {"timestamp": "b", "total": 10, "passed": 5}])
    before = sorted(os.listdir(logs))
    assert se.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would raise v4 memory eval recall fell to 5/10" in out and "1 finding(s), nothing written" in out
    assert sorted(os.listdir(logs)) == before


def test_main_writes_an_event_and_one_work_item_and_dedupes(logs, capsys):
    jwrite(logs / "memory_eval.jsonl", [{"timestamp": "a", "total": 10, "passed": 10}, {"timestamp": "b", "total": 10, "passed": 5}])
    assert se.main([]) == 0
    assert "1 finding(s), 1 new intent(s)" in capsys.readouterr().out
    intents = intent_store.read_jsonl(intent_store.INTENTS, str(logs))
    assert len(intents) == 1 and intents[0]["kind"] == "self_regression" and intents[0]["tier"] == 1 and intents[0]["value"] == 4
    assert len(intent_store.read_jsonl(intent_store.EVENTS, str(logs))) == 1
    assert "sense_self:" in (logs / "alerts.txt").read_text(encoding="utf-8")
    assert se.main([]) == 0
    assert "1 finding(s), 0 new intent(s)" in capsys.readouterr().out
    assert len(intent_store.read_jsonl(intent_store.INTENTS, str(logs))) == 1


def test_a_clean_run_writes_no_files(logs, capsys):
    assert se.main([]) == 0
    assert "0 finding(s), 0 new intent(s)" in capsys.readouterr().out
    assert not (logs / "intents.jsonl").exists() and not (logs / "alerts.txt").exists()


def test_kill_switch(logs, capsys):
    (logs / se.OFF_NAME).write_text("off", encoding="utf-8")
    jwrite(logs / "memory_eval.jsonl", [{"total": 10, "passed": 10}, {"total": 10, "passed": 1}])
    assert se.main([]) == 0 and "off" in capsys.readouterr().out
    assert not (logs / "intents.jsonl").exists()


def test_a_bad_claim_hours_value_is_reported_not_a_crash(logs, capsys):
    assert se.main(["--claim-hours", "soon"]) == 0 and "whole number" in capsys.readouterr().out
    assert se.main(["--claim-hours"]) == 0


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    jwrite(root / "logs" / "memory_eval.jsonl", [{"timestamp": "a", "total": 10, "passed": 9}, {"timestamp": "b", "total": 10, "passed": 4}])
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "sense_self.py")], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "1 finding(s), 1 new intent(s)" in r.stdout
    assert len(intent_store.read_jsonl(intent_store.INTENTS, str(root / "logs"))) == 1
