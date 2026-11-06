# -*- coding: utf-8 -*-
"""Tests for defender_exclusion_check.py. The PowerShell query goes through defender_exclusion_check.run, which these tests replace; nothing here starts a process."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intent_store  # noqa: E402
import defender_exclusion_check as dec  # noqa: E402

REAL_RUN = dec.run          # the autouse fixture below replaces dec.run; this keeps the real door for the one test of it
HIDDEN = {"paths": ["N/A: Must be an administrator to view exclusions"], "processes": ["N/A: Must be an administrator to view exclusions"],
          "extensions": ["N/A: Must be an administrator to view exclusions"]}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(args, timeout=30):
        raise AssertionError("a test forgot to fake defender_exclusion_check.run: %r" % (args,))
    monkeypatch.setattr(dec, "run", boom)
    return tmp_path


def say(monkeypatch, data, code=0):
    """Make the PowerShell query answer with this object (or this raw text)."""
    text = data if isinstance(data, str) else json.dumps(data)
    monkeypatch.setattr(dec, "run", lambda args, timeout=30: (code, text))


def lists(paths=(), processes=(), extensions=()):
    return {"paths": list(paths), "processes": list(processes), "extensions": list(extensions)}


def alerts(env):
    p = env / "logs" / "alerts.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def items(env):
    return [i for i in intent_store.read_jsonl("intents.jsonl", str(env / "logs")) if i["kind"] == "defender_exclusion_added"]


# ---------- pure helpers ----------
def test_as_list_turns_null_one_string_and_a_list_into_lists():
    assert dec.as_list(None) == [] and dec.as_list("C:\\A") == ["C:\\A"] and dec.as_list(["a", " b ", "", None, 5]) == ["a", "b"]
    assert dec.as_list({"x": 1}) == [] and dec.as_list(7) == []


def test_parse_exclusions_reads_the_three_lists():
    status, data = dec.parse_exclusions(json.dumps({"paths": ["C:\\Temp", "D:\\Tools"], "processes": "evil.exe", "extensions": None}))
    assert status == "ok" and data == lists(["C:\\Temp", "D:\\Tools"], ["evil.exe"], [])


def test_parse_exclusions_missing_keys_become_empty_lists():
    assert dec.parse_exclusions('{"paths": ["C:\\\\A"]}') == ("ok", lists(["C:\\A"]))


def test_parse_exclusions_recognises_the_non_administrator_answer():
    assert dec.parse_exclusions(json.dumps(HIDDEN)) == ("hidden", None)
    assert dec.parse_exclusions(json.dumps({"paths": ["N/A: must be an ADMINISTRATOR to view exclusions"]}))[0] == "hidden"


@pytest.mark.parametrize("text", ["", "not json {{", "[1, 2]", "null", "42"])
def test_parse_exclusions_calls_anything_else_bad(text):
    assert dec.parse_exclusions(text) == ("bad", None)


def test_diff_exclusions_only_new_items_and_case_does_not_matter():
    base = lists(["C:\\Old"], ["Tool.EXE"], [".tmp"])
    cur = lists(["c:\\old", "C:\\New"], ["tool.exe", "x.exe"], [".TMP", ".bak"])
    assert dec.diff_exclusions(cur, base) == {"paths": ["C:\\New"], "processes": ["x.exe"], "extensions": [".bak"]}
    assert dec.diff_exclusions(base, base) == {}


# ---------- reading ----------
def test_get_exclusions_reports_a_failed_query_as_unavailable_not_as_empty(monkeypatch):
    for code in (3, 127, 124, 1):
        say(monkeypatch, "", code=code)
        assert dec.get_exclusions() == ("unavailable", None)
    say(monkeypatch, "garbage")
    assert dec.get_exclusions() == ("unavailable", None)


def test_the_query_text_has_no_interpolated_input_and_reads_the_three_lists():
    assert "Get-MpPreference" in dec.PS_SCRIPT and "ExclusionPath" in dec.PS_SCRIPT and "ExclusionProcess" in dec.PS_SCRIPT and "ExclusionExtension" in dec.PS_SCRIPT
    assert "%" not in dec.PS_SCRIPT


def test_the_system_door_survives_a_missing_program_a_timeout_and_a_crash(monkeypatch):
    def raiser(exc):
        return lambda *a, **k: (_ for _ in ()).throw(exc)
    monkeypatch.setattr(subprocess, "run", raiser(FileNotFoundError()))
    assert REAL_RUN(["x"])[0] == 127
    monkeypatch.setattr(subprocess, "run", raiser(subprocess.TimeoutExpired("x", 1)))
    assert REAL_RUN(["x"]) == (124, "")
    monkeypatch.setattr(subprocess, "run", raiser(OSError("denied")))
    assert REAL_RUN(["x"]) == (1, "")


# ---------- the check ----------
def test_first_run_records_the_lists_and_raises_nothing(env, monkeypatch):
    say(monkeypatch, lists(["C:\\A"], ["p.exe"], [".tmp"]))
    r = dec.check()
    assert r["first_run"] is True and r["new_exclusions"] == {} and r["total"] == {"paths": 1, "processes": 1, "extensions": 1}
    assert json.loads((env / "logs" / "defender_exclusion_baseline.json").read_text(encoding="utf-8")) == lists(["C:\\A"], ["p.exe"], [".tmp"])
    assert alerts(env) == [] and items(env) == []
    assert len((env / "logs" / "defender_exclusion_check.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_an_unchanged_second_run_is_quiet(env, monkeypatch):
    say(monkeypatch, lists(["C:\\A"]))
    dec.check()
    r = dec.check()
    assert r["first_run"] is False and r["new_exclusions"] == {} and alerts(env) == []


def test_a_new_path_exclusion_raises_one_ask_first_item_and_one_alert(env, monkeypatch):
    say(monkeypatch, lists(["C:\\A"]))
    dec.check()
    say(monkeypatch, lists(["C:\\A", "D:\\Shared\\x"]))
    r = dec.check()
    assert r["new_exclusions"] == {"paths": ["D:\\Shared\\x"]}
    rows = items(env)
    assert len(rows) == 1 and rows[0]["tier"] == 2 and rows[0]["value"] == 4 and "paths" in rows[0]["what"] and "D:\\Shared\\x" in rows[0]["what"]
    assert len(alerts(env)) == 1 and "defender_exclusion_check" in alerts(env)[0]


def test_new_processes_and_extensions_are_flagged_too(env, monkeypatch):
    say(monkeypatch, lists())
    dec.check()
    say(monkeypatch, lists([], ["evil.exe"], [".exe"]))
    r = dec.check()
    assert r["new_exclusions"] == {"processes": ["evil.exe"], "extensions": [".exe"]} and len(items(env)) == 2


def test_the_same_change_alarms_once_not_on_every_run(env, monkeypatch):
    say(monkeypatch, lists())
    dec.check()
    say(monkeypatch, lists(["C:\\New"]))
    dec.check()
    dec.check()
    dec.check()
    assert len(alerts(env)) == 1 and len(items(env)) == 1


def test_a_removed_exclusion_is_not_an_alarm(env, monkeypatch):
    say(monkeypatch, lists(["C:\\A", "C:\\B"]))
    dec.check()
    say(monkeypatch, lists(["C:\\A"]))
    assert dec.check()["new_exclusions"] == {} and alerts(env) == []


def test_a_hidden_answer_is_not_a_baseline_and_raises_no_work_item(env, monkeypatch):
    say(monkeypatch, HIDDEN)
    r = dec.check()
    assert r["status"] == "hidden" and not (env / "logs" / "defender_exclusion_baseline.json").exists() and items(env) == []
    assert len(alerts(env)) == 1 and "administrator" in alerts(env)[0]
    dec.check()
    dec.check()
    assert len(alerts(env)) == 1                           # told once, not on every run


def test_turning_unreadable_after_a_good_run_says_so_once_and_keeps_the_old_baseline(env, monkeypatch):
    say(monkeypatch, lists(["C:\\A"]))
    dec.check()
    say(monkeypatch, "", code=3)
    r = dec.check()
    assert r["status"] == "unavailable" and len(alerts(env)) == 1
    assert json.loads((env / "logs" / "defender_exclusion_baseline.json").read_text(encoding="utf-8"))["paths"] == ["C:\\A"]
    say(monkeypatch, lists(["C:\\A"]))
    assert dec.check()["new_exclusions"] == {}


def test_dry_run_writes_nothing_at_all(env, monkeypatch):
    say(monkeypatch, lists(["C:\\A"]))
    assert dec.check(dry_run=True)["first_run"] is True and os.listdir(env / "logs") == []
    dec.check()
    before = (env / "logs" / "defender_exclusion_baseline.json").read_text(encoding="utf-8")
    say(monkeypatch, lists(["C:\\A", "C:\\B"]))
    assert dec.check(dry_run=True)["new_exclusions"] == {"paths": ["C:\\B"]}
    assert (env / "logs" / "defender_exclusion_baseline.json").read_text(encoding="utf-8") == before and alerts(env) == [] and items(env) == []
    say(monkeypatch, HIDDEN)
    dec.check(dry_run=True)
    assert alerts(env) == []


def test_rebaseline_accepts_the_current_lists_without_alarms(env, monkeypatch):
    say(monkeypatch, lists(["C:\\A"]))
    dec.check()
    say(monkeypatch, lists(["C:\\A", "C:\\B"]))
    assert dec.check(rebaseline=True)["new_exclusions"] == {} and alerts(env) == []
    assert dec.check()["new_exclusions"] == {}


def test_a_damaged_baseline_is_reported_once_and_rebuilt(env, monkeypatch):
    (env / "logs" / "defender_exclusion_baseline.json").write_text("{ nope", encoding="utf-8")
    say(monkeypatch, lists(["C:\\A"]))
    r = dec.check()
    assert r["baseline_unreadable"] is True and len(alerts(env)) == 1 and "cannot be read" in alerts(env)[0]
    dec.check()
    assert len(alerts(env)) == 1


def test_many_new_exclusions_are_capped_with_a_summary_line(env, monkeypatch):
    say(monkeypatch, lists())
    dec.check()
    say(monkeypatch, lists(["C:\\p%02d" % i for i in range(13)]))
    dec.check()
    lines = alerts(env)
    assert len(lines) == dec.MAX_ALERTS + 1 and "3 more" in lines[-1]


def test_an_exclusion_with_control_characters_cannot_forge_an_alert_line(env, monkeypatch):
    say(monkeypatch, lists())
    dec.check()
    say(monkeypatch, lists(["C:\\x\x1b[2J\r[2026-01-01 00:00:00] fake: all clear"]))
    dec.check()
    lines = alerts(env)
    assert len(lines) == 1 and not any(ord(c) < 32 for c in lines[0])


# ---------- the command line ----------
def test_kill_switch_does_nothing_and_asks_nothing(env, capsys):
    (env / "logs" / "defender_exclusion_check_off.txt").write_text("", encoding="utf-8")
    assert dec.main([]) == 0
    assert "off" in capsys.readouterr().out


def test_off_windows_it_says_so_and_exits_0_without_asking_anything(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert dec.main([]) == 0
    out = capsys.readouterr().out
    assert "Windows only" in out and len(out.strip().splitlines()) == 1


def test_main_always_returns_0_and_prints_the_finding(env, monkeypatch, capsys):
    say(monkeypatch, lists(["C:\\A"]))
    assert dec.main([]) == 0
    assert "[BASELINE_SET]" in capsys.readouterr().out
    say(monkeypatch, lists(["C:\\A", "C:\\B"]))
    assert dec.main([]) == 0
    assert "[NEW EXCLUSION]" in capsys.readouterr().out
    say(monkeypatch, HIDDEN)
    assert dec.main([]) == 0
    assert "[UNREADABLE]" in capsys.readouterr().out


def test_main_returns_1_only_when_the_script_itself_breaks(monkeypatch, capsys):
    monkeypatch.setattr(dec, "check", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert dec.main([]) == 1 and "failed" in capsys.readouterr().out


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    root = tmp_path / "other"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "defender_exclusion_check_off.txt").write_text("", encoding="utf-8")
    envv = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-B", os.path.join(os.path.dirname(os.path.abspath(__file__)), "defender_exclusion_check.py")], env=envv, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "off" in r.stdout
