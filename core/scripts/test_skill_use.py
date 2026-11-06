# -*- coding: utf-8 -*-
"""Tests for skill_use.py: finding the transcripts, counting Skill calls, days, the nudge follow-up and the command line."""
import calendar
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import skill_use as su  # noqa: E402


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".123Z"


def local_stamp(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def skill_row(ts, skill):
    return {"timestamp": ts, "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": skill}}]}}


def write_jsonl(path, rows, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([json.dumps(r) for r in rows] + list(extra_lines)) + "\n", encoding="utf-8")
    return path


def test_project_slug_turns_every_non_alphanumeric_into_a_dash():
    assert su.project_slug("/home/sam/my assistant") == "-home-sam-my-assistant"
    assert su.project_slug("Q" + ":\\Work\\Helper_1") == "Q--Work-Helper-1"


def test_transcripts_dir_uses_the_slug_under_the_config_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_TRANSCRIPTS", raising=False)
    slug = su.project_slug("/some/project")
    (tmp_path / "projects" / slug).mkdir(parents=True)
    assert su.transcripts_dir("/some/project", tmp_path) == tmp_path / "projects" / slug


def test_transcripts_dir_matches_the_slug_case_insensitively(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_TRANSCRIPTS", raising=False)
    (tmp_path / "projects" / "C--WORK-HELPER").mkdir(parents=True)
    got = su.transcripts_dir("c:/work/helper", tmp_path)
    assert got.is_dir() and got.name.lower() == "c--work-helper"      # on a case-insensitive disk the exact path already works; elsewhere the scan finds it


def test_transcripts_dir_for_an_unknown_project_is_a_path_that_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_TRANSCRIPTS", raising=False)
    got = su.transcripts_dir("/nowhere", tmp_path)
    assert not got.exists() and got.parent == tmp_path / "projects"


def test_environment_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TRANSCRIPTS", str(tmp_path / "mine"))
    assert su.transcripts_dir("/anything", tmp_path) == tmp_path / "mine"


def test_config_dir_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert su.config_dir() == tmp_path / "cfg"


def test_transcript_files_returns_the_newest_first_and_respects_the_limit(tmp_path):
    for i in range(4):
        p = tmp_path / ("s%d.jsonl" % i)
        p.write_text("{}", encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert [p.name for p in su.transcript_files(tmp_path, limit=2)] == ["s3.jsonl", "s2.jsonl"]
    assert su.transcript_files(tmp_path / "missing") == []


def test_invocations_reads_only_skill_tool_calls(tmp_path):
    rows = [
        skill_row("2026-10-29T01:00:00Z", "harness"),
        {"timestamp": "2026-10-29T01:00:05Z", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "echo Skill"}}]}},
        {"timestamp": "2026-10-29T01:00:09Z", "message": {"content": "plain string"}},
        {"timestamp": "2026-10-29T01:00:10Z", "message": {"content": [{"type": "text", "text": "I will use the \"Skill\" tool"}]}},
    ]
    p = write_jsonl(tmp_path / "t.jsonl", rows, ['garbage line with "name":"Skill" in it', '"Skill"', "[1, 2]", '{"message": 5, "x": "Skill"}'])
    assert su.invocations([p]) == [("2026-10-29T01:00:00Z", "harness")]


def test_invocations_handles_missing_files_odd_inputs_and_unreadable_bytes(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_bytes(b'\xff\xfe junk "Skill"\n' + json.dumps(skill_row("2026-10-29T02:00:00Z", "a")).encode("utf-8") + b"\n")
    odd = {"timestamp": None, "message": {"content": [{"type": "tool_use", "name": "Skill", "input": "not a dict"}]}}
    q = write_jsonl(tmp_path / "u.jsonl", [odd])
    got = su.invocations([tmp_path / "missing.jsonl", p, q])
    assert ("2026-10-29T02:00:00Z", "a") in got and ("", "?") in got


def test_utc_epoch_is_exact_and_tolerates_junk():
    assert su.utc_epoch("2026-10-29T01:00:00Z") == calendar.timegm((2026, 10, 29, 1, 0, 0))
    assert su.utc_epoch("2026-10-29T01:00:00.999Z") == su.utc_epoch("2026-10-29T01:00:00Z")
    assert su.utc_epoch("") == 0.0 and su.utc_epoch("nonsense") == 0.0 and su.utc_epoch(None) == 0.0


def test_count_by_day_uses_local_days_and_skips_unreadable_stamps():
    e = calendar.timegm((2026, 10, 29, 12, 0, 0))
    rows = [(iso(e), "a"), (iso(e + 60), "b"), (iso(e + 3 * 86400), "a"), ("", "c"), ("junk", "d")]
    counts = su.count_by_day(rows)
    assert counts[time.strftime("%Y-%m-%d", time.localtime(e))] == 2
    assert sum(counts.values()) == 3


def test_count_by_skill():
    assert su.count_by_skill([("t", "a"), ("t", "b"), ("t", "a")]) == {"a": 2, "b": 1}


def test_followed_counts_only_the_nudged_skill_inside_the_window():
    base = calendar.timegm((2026, 10, 29, 10, 0, 0))
    invs = [(iso(base + 300), "harness"), (iso(base + 5000), "vault-hygiene")]
    nudges = [(local_stamp(base), ["harness", "vault-hygiene", "other"])]
    assert su.followed(nudges, invs, window_s=900) == (3, 1)


def test_a_skill_loaded_before_the_nudge_does_not_count():
    base = calendar.timegm((2026, 10, 29, 10, 0, 0))
    assert su.followed([(local_stamp(base), ["harness"])], [(iso(base - 60), "harness")]) == (1, 0)


def test_followed_skips_unreadable_nudge_stamps():
    assert su.followed([("not a time", ["a"])], []) == (0, 0)


def test_read_nudges_reads_good_rows_and_skips_bad_ones(tmp_path):
    p = tmp_path / "skill_nudge.jsonl"
    p.write_text('{"ts": "2026-10-29 10:00:00", "skills": ["a", "b"]}\n{bad\n{"ts": 5, "skills": []}\n{"skills": ["x"]}\n[]\n', encoding="utf-8")
    assert su.read_nudges(p) == [("2026-10-29 10:00:00", ["a", "b"])]
    assert su.read_nudges(tmp_path / "missing.jsonl") == []


def test_report_lists_today_recent_days_top_skills_and_the_follow_up_when_there_are_nudges():
    e = calendar.timegm((2026, 10, 29, 12, 0, 0))
    rows = [(iso(e), "harness"), (iso(e + 10), "harness"), (iso(e + 20), "vault-hygiene")]
    day = time.strftime("%Y-%m-%d", time.localtime(e))
    lines = su.report(rows, [(local_stamp(e - 5), ["harness"])], days=7, today=day)
    text = "\n".join(lines)
    assert "today (%s): 3" % day in text and "harness=2" in text and "vault-hygiene=1" in text and "nudges: 1, followed by that skill within 15 min: 1" in text


def test_report_with_no_data_and_no_nudges_says_none():
    lines = su.report([], [], today="2026-10-29")
    assert lines[0].endswith(": 0") and "none" in lines[1] and "none" in lines[2] and len(lines) == 3


def test_main_prints_a_report_for_a_transcripts_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    write_jsonl(tmp_path / "t" / "s.jsonl", [skill_row(iso(time.time()), "harness")])
    assert su.main(["--dir", str(tmp_path / "t")]) == 0
    out = capsys.readouterr().out
    assert "today" in out and "harness=1" in out and "nudges" not in out


def test_main_with_no_transcripts_prints_a_hint_and_exits_zero(tmp_path, capsys):
    assert su.main(["--dir", str(tmp_path / "nothing")]) == 0
    assert "no transcripts found" in capsys.readouterr().out


def test_real_process_runs_and_exits_zero(tmp_path):
    write_jsonl(tmp_path / "t" / "s.jsonl", [skill_row(iso(time.time()), "harness")])
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "skill_use.py"), "--dir", str(tmp_path / "t")], capture_output=True, env=env, timeout=30)
    assert r.returncode == 0 and b"harness=1" in r.stdout
