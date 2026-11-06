# -*- coding: utf-8 -*-
"""Tests for token_usage_report.py: settings (time zone, weekly reset), the weekly window, the log scan, the sums and the real process."""
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import token_usage_report as t  # noqa: E402

NOW = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)      # a Wednesday
PLUS8 = timezone(timedelta(hours=8))


def arow(mid, ts, model="model-a", usage=(1, 2, 3, 4), tools=()):
    content = [{"type": "tool_use", "id": "tu_%s_%d" % (mid, i), "name": n, "input": {}} for i, n in enumerate(tools)]
    content = content or [{"type": "text", "text": "hi"}]
    u = dict(zip(t.FIELDS, usage))
    return {"type": "assistant", "uuid": "u-" + mid, "timestamp": ts, "message": {"id": mid, "model": model, "content": content, "usage": u}}


def write_log(path, rows, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        for line in extra_lines:
            f.write(line + "\n")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake Claude config folder with a projects folder, and an empty logs folder."""
    cfg = tmp_path / "claude-config"
    proj = cfg / "projects"
    proj.mkdir(parents=True)
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(t, "LOGS", logs)
    return {"cfg": cfg, "projects": proj, "logs": logs}


# ---------------------------------------------------------------- small helpers

def test_parse_ts_accepts_z_offset_and_naive():
    assert t.parse_ts("2026-03-04T10:00:00Z") == datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc)
    assert t.parse_ts("2026-03-04T10:00:00+02:00").utcoffset() == timedelta(hours=2)
    assert t.parse_ts("2026-03-04T10:00:00").tzinfo == timezone.utc


@pytest.mark.parametrize("bad", [None, "", "yesterday", 5, "2026-13-45T00:00:00Z"])
def test_parse_ts_rejects_junk(bad):
    assert t.parse_ts(bad) is None


def test_to_int_and_formatting():
    assert t.to_int("7") == 7 and t.to_int(None) == 0 and t.to_int("x") == 0 and t.to_int([1]) == 0
    assert t.fmt(999) == "999" and t.fmt(1500) == "1.5K" and t.fmt(2_500_000) == "2.50M" and t.fmt(3e9) == "3.00B"
    assert t.pct(1, 4) == "25.0%" and t.pct(1, 0) == "0.0%"


def test_table_aligns_and_right_justifies():
    out = t.table(["a", "bb"], [["x", 1], ["yyy", 22]], right=(1,))
    lines = out.splitlines()
    assert lines[0].startswith("a  ") and lines[1].startswith("---")
    assert lines[2].endswith(" 1") and lines[3].endswith("22")


# ---------------------------------------------------------------- settings

def test_zone_local_utc_and_offsets():
    assert t.parse_zone(None) == (None, "local time") and t.parse_zone("local")[0] is None
    assert t.parse_zone("UTC") == (timezone.utc, "UTC")
    z, label = t.parse_zone("+08:00")
    assert z.utcoffset(None) == timedelta(hours=8) and label == "UTC+08:00"
    assert t.parse_zone("UTC-5")[0].utcoffset(None) == timedelta(hours=-5)
    assert t.parse_zone("-0530")[0].utcoffset(None) == -timedelta(hours=5, minutes=30)
    assert t.parse_zone("gmt+1")[1] == "UTC+01:00"


@pytest.mark.parametrize("bad", ["+15:00", "+14:30", "+9:99", "Not/AZone", "../../etc/passwd", "/etc/localtime", "banana"])
def test_zone_rejects_nonsense_and_path_tricks(bad):
    with pytest.raises(t.ConfigError):
        t.parse_zone(bad)


def test_zone_iana_name_when_tzdata_exists():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Europe/Paris")
    except Exception:
        pytest.skip("no tz database on this machine")
    z, label = t.parse_zone("Europe/Paris")
    assert label == "Europe/Paris" and z.utcoffset(datetime(2026, 1, 1)) == timedelta(hours=1)


@pytest.mark.parametrize("value,expected", [("monday", 0), ("Mon", 0), ("SUN", 6), ("sunday", 6), (3, 3), ("4", 4), ("wed", 2)])
def test_parse_day(value, expected):
    assert t.parse_day(value) == expected


@pytest.mark.parametrize("bad", [None, True, "", "7", -1, "funday", "mo", 9, 2.5])
def test_parse_day_rejects(bad):
    with pytest.raises(t.ConfigError):
        t.parse_day(bad)


def test_parse_time_forms_and_limits():
    assert t.parse_time(10) == (10, 0) and t.parse_time("10:30") == (10, 30) and t.parse_time("7", 15) == (7, 15)
    for bad in [(24, None), (-1, None), ("ten", None), ("10:61", None), (5, 60), (None, None), (True, None)]:
        with pytest.raises(t.ConfigError):
            t.parse_time(*bad)


def test_config_missing_is_empty_and_bad_files_are_reported(env):
    assert t.load_config() == {}
    p = env["logs"] / t.CONFIG_NAME
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(t.ConfigError, match="not valid JSON"):
        t.load_config()
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(t.ConfigError, match="JSON object"):
        t.load_config()
    p.write_text('﻿{"timezone": "UTC"}', encoding="utf-8")       # a file saved by Notepad with a byte-order mark
    assert t.load_config() == {"timezone": "UTC"}


def test_resolve_settings_arguments_beat_the_file():
    cfg = {"timezone": "+02:00", "reset_day": "friday", "reset_hour": 8}
    s = t.resolve_settings(tz="UTC", reset_day="mon", reset_hour="9", reset_minute="30", cfg=cfg)
    assert s["tz_label"] == "UTC" and s["reset"] == (0, 9, 30)
    s = t.resolve_settings(cfg=cfg)
    assert s["tz_label"] == "UTC+02:00" and s["reset"] == (4, 8, 0)


def test_resolve_settings_has_no_personal_default():
    s = t.resolve_settings()
    assert s["tz"] is None and s["reset"] is None


@pytest.mark.parametrize("kw", [{"reset_day": "monday"}, {"reset_hour": 10}, {"reset_minute": 30}])
def test_resolve_settings_needs_day_and_hour_together(kw):
    with pytest.raises(t.ConfigError):
        t.resolve_settings(**kw)


# ---------------------------------------------------------------- the weekly window

def test_weekly_start_after_the_reset_on_the_reset_day():
    now = datetime(2026, 3, 2, 3, 0, tzinfo=timezone.utc)                  # Monday 11:00 at +08:00
    assert t.weekly_start(now, PLUS8, 0, 10) == datetime(2026, 3, 2, 10, 0, tzinfo=PLUS8)


def test_weekly_start_before_the_reset_on_the_reset_day_goes_back_a_week():
    now = datetime(2026, 3, 2, 1, 0, tzinfo=timezone.utc)                  # Monday 09:00 at +08:00
    assert t.weekly_start(now, PLUS8, 0, 10) == datetime(2026, 2, 23, 10, 0, tzinfo=PLUS8)


def test_weekly_start_midweek_and_other_reset_days():
    assert t.weekly_start(NOW, timezone.utc, 0, 10) == datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)
    assert t.weekly_start(NOW, timezone.utc, 6, 0, 30) == datetime(2026, 3, 1, 0, 30, tzinfo=timezone.utc)      # Sunday
    assert t.weekly_start(NOW, timezone.utc, 2, 16) == datetime(2026, 2, 25, 16, 0, tzinfo=timezone.utc)        # today's reset is still ahead


def test_weekly_start_in_the_machine_zone_lands_on_the_reset_day_and_hour():
    s = t.weekly_start(NOW, None, 1, 7, 15)
    local = s.astimezone()
    assert (local.weekday(), local.hour, local.minute) == (1, 7, 15) and s <= NOW and NOW - s < timedelta(days=7, hours=1)


def test_weekly_start_keeps_the_wall_clock_across_a_clock_change():
    try:
        from zoneinfo import ZoneInfo
        paris = ZoneInfo("Europe/Paris")
    except Exception:
        pytest.skip("no tz database on this machine")
    now = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)                  # after the March clock change
    s = t.weekly_start(now, paris, 0, 10)                                   # Monday 30 March
    assert (s.year, s.month, s.day, s.hour) == (2026, 3, 30, 10) and s.utcoffset() == timedelta(hours=2)
    before = t.weekly_start(datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc), paris, 0, 10)   # Monday 23 March, still winter time
    assert before.hour == 10 and before.utcoffset() == timedelta(hours=1)


def test_window_without_a_reset_is_a_rolling_week_and_says_so():
    s = t.resolve_settings(tz="UTC")
    start, label, note = t.window_for(None, NOW, s)
    assert start == NOW - timedelta(days=7) and "rolling" in label and "no weekly reset is configured" in note
    assert "monday" not in (label + note).lower()


def test_window_with_a_reset_names_the_day_and_zone():
    s = t.resolve_settings(tz="+08:00", reset_day="mon", reset_hour=10)
    start, label, note = t.window_for(None, NOW, s)
    assert start == datetime(2026, 3, 2, 10, 0, tzinfo=PLUS8) and note == ""
    assert "Monday 10:00" in label and "UTC+08:00" in label


def test_days_option_overrides_the_weekly_window():
    s = t.resolve_settings(tz="UTC", reset_day="mon", reset_hour=10)
    start, label, note = t.window_for(2, NOW, s)
    assert start == NOW - timedelta(days=2) and label.startswith("last 2 days") and note == ""


# ---------------------------------------------------------------- reading the logs

def test_scan_keeps_the_largest_usage_per_message_id(env):
    rows = [arow("m1", "2026-03-04T10:00:00Z", usage=(1, 0, 0, 1)), arow("m1", "2026-03-04T10:00:01Z", usage=(1, 0, 0, 50)),
            arow("m2", "2026-03-04T11:00:00Z", usage=(5, 5, 5, 5))]
    write_log(env["projects"] / "proj" / "s1.jsonl", rows)
    best, read, total, dups = t.scan(env["projects"], datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert best["m1"]["vals"] == (1, 0, 0, 50) and best["m2"]["vals"] == (5, 5, 5, 5) and (read, total, dups) == (1, 1, 0)


def test_scan_counts_an_id_once_across_files_and_reports_it(env):
    row = arow("dup", "2026-03-04T10:00:00Z", usage=(2, 2, 2, 2))
    write_log(env["projects"] / "p" / "a.jsonl", [row])
    write_log(env["projects"] / "p" / "b.jsonl", [row])
    best, read, total, dups = t.scan(env["projects"], datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert len(best) == 1 and dups == 1 and read == 2


def test_scan_skips_malformed_lines_and_rows_without_usage_or_time(env):
    extra = ["{broken json with \"assistant\"", "[1, 2, \"assistant\"]", json.dumps({"type": "assistant", "message": "text"})]
    nousage = arow("nu", "2026-03-04T10:00:00Z")
    del nousage["message"]["usage"]
    notime = arow("nt", "")
    write_log(env["projects"] / "p" / "a.jsonl", [arow("ok", "2026-03-04T10:00:00Z"), nousage, notime], extra)
    best, _r, _t, _d = t.scan(env["projects"], datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert list(best) == ["ok"]


def test_scan_skips_files_untouched_since_before_the_window(env):
    p = write_log(env["projects"] / "p" / "old.jsonl", [arow("o", "2026-03-04T10:00:00Z")])
    old = (datetime(2026, 3, 1, tzinfo=timezone.utc) - timedelta(days=3)).timestamp()
    os.utime(p, (old, old))
    best, read, total, _d = t.scan(env["projects"], datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert best == {} and read == 0 and total == 1


def test_scan_drops_messages_before_the_window_start(env):
    write_log(env["projects"] / "p" / "a.jsonl", [arow("early", "2026-03-04T09:59:59Z"), arow("late", "2026-03-04T10:00:00Z")])
    best, *_ = t.scan(env["projects"], datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc))
    assert list(best) == ["late"]


def test_scan_records_tool_names_and_finds_subagent_logs(env):
    write_log(env["projects"] / "p" / "s1" / "subagents" / "agent-abcdef1234.jsonl", [arow("sa", "2026-03-04T10:00:00Z", tools=("Bash", "Read"))])
    best, *_ = t.scan(env["projects"], datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert best["sa"]["tools"] == ["Bash", "Read"]
    assert t.session_name(best["sa"]["file"]) == ("sub abcdef12", "p", True)
    assert t.session_name(os.path.join("proj", "1234567890abcdef.jsonl")) == ("12345678", "proj", False)


def test_an_over_long_line_is_skipped_not_loaded(env, monkeypatch):
    monkeypatch.setattr(t, "MAX_LINE", 2000)
    huge = json.dumps(arow("big", "2026-03-04T10:00:00Z", model="x" * 5000))
    p = env["projects"] / "p" / "a.jsonl"
    write_log(p, [arow("a", "2026-03-04T10:00:00Z")], [huge])
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(arow("b", "2026-03-04T10:00:01Z")) + "\n")
    best, *_ = t.scan(env["projects"], datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert sorted(best) == ["a", "b"]


def test_iter_lines_keeps_a_last_line_without_newline():
    got = list(t.iter_lines(io.BytesIO(b"one\ntwo"), cap=100))
    assert got == [b"one\n", b"two"]


def test_find_logs_ignores_other_files_and_missing_folder(env, tmp_path):
    (env["projects"] / "p").mkdir()
    (env["projects"] / "p" / "notes.txt").write_text("x")
    write_log(env["projects"] / "p" / "a.jsonl", [])
    assert [os.path.basename(x) for x in t.find_logs(env["projects"])] == ["a.jsonl"]
    assert t.find_logs(tmp_path / "nope") == []


# ---------------------------------------------------------------- sums

def test_summarize_totals_models_sessions_and_tool_split():
    ts = datetime(2026, 3, 4, tzinfo=timezone.utc)
    rows = [{"file": "p/a.jsonl", "model": "m1", "ts": ts, "vals": (1, 2, 3, 4), "tools": ["Bash", "Read"]},
            {"file": "p/a.jsonl", "model": "m2", "ts": ts - timedelta(hours=1), "vals": (10, 0, 0, 0), "tools": []},
            {"file": "p/b.jsonl", "model": "m1", "ts": ts, "vals": (0, 0, 5, 5), "tools": ["Bash"]}]
    s = t.summarize(rows)
    assert s["vals"] == [11, 2, 8, 9] and s["n"] == 3
    assert sum(sum(v) for v in s["models"].values()) == sum(s["vals"]) == sum(sum(x["vals"]) for x in s["sessions"].values())
    assert s["tool_calls"]["Bash"] == 2 and s["tool_est"]["Bash"] == pytest.approx(5 + 10) and s["tool_est"]["Read"] == pytest.approx(5)
    assert s["tool_turn_tokens"] == 20 and s["sessions"]["p/a.jsonl"]["first"] == ts - timedelta(hours=1)


def test_in_window_bounds():
    a = {"ts": datetime(2026, 3, 1, tzinfo=timezone.utc)}
    b = {"ts": datetime(2026, 3, 3, tzinfo=timezone.utc)}
    start, end = datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 3, tzinfo=timezone.utc)
    assert list(t.in_window([a, b], start)) == [a, b] and list(t.in_window([a, b], start, end)) == [a]


# ---------------------------------------------------------------- the whole command

def fill(env):
    write_log(env["projects"] / "proj-one" / "aaaaaaaa-1111.jsonl", [
        arow("before", "2026-03-02T01:59:00Z", usage=(100, 0, 0, 0)),                    # Monday 09:59 at +08:00, before the reset
        arow("w1", "2026-03-02T02:01:00Z", usage=(1, 10, 100, 5), tools=("Bash",)),      # just after the reset
        arow("w2", "2026-03-04T10:00:00Z", model="model-b", usage=(2, 0, 50, 7)),        # today
    ])


def run_main(argv, now=NOW):
    return t.main(argv, now=now)


def test_main_weekly_window_from_arguments(env, capsys):
    fill(env)
    code = run_main(["--tz", "+08:00", "--reset-day", "monday", "--reset-hour", "10", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["checks"]["consistent"] is True
    assert out["window"]["messages"] == 2 and out["window"]["total"] == 1 + 10 + 100 + 5 + 2 + 50 + 7     # the 09:59 message is excluded
    assert out["weekly_reset"] == {"day": "monday", "hour": 10, "minute": 0} and out["timezone"] == "UTC+08:00"
    assert out["window"]["start"].startswith("2026-03-02T10:00:00+08:00")
    assert out["today"]["messages"] == 1 and out["files"] == {"found": 1, "read": 1}


def test_main_reads_the_settings_file(env, capsys):
    fill(env)
    (env["logs"] / t.CONFIG_NAME).write_text(json.dumps({"timezone": "UTC", "reset_day": 2, "reset_hour": 9}), encoding="utf-8")
    code = run_main(["--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["window"]["start"].startswith("2026-03-04T09:00:00+00:00") and out["window"]["messages"] == 1


def test_main_without_settings_shows_a_rolling_week_and_no_utc8(env, capsys):
    fill(env)
    code = run_main([])
    text = capsys.readouterr().out
    assert code == 0 and "rolling" in text and "no weekly reset is configured" in text.lower()
    assert "UTC+8" not in text and "Monday" not in text and "TOTALS" in text and "Checks:" in text and "OK" in text


def test_main_days_option(env, capsys):
    fill(env)
    assert run_main(["--days", "1", "--tz", "UTC", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["window"]["messages"] == 1


def test_main_text_report_names_the_zone_and_top_lists(env, capsys):
    fill(env)
    run_main(["--tz", "+08:00", "--reset-day", "mon", "--reset-hour", "10", "--top", "3"])
    text = capsys.readouterr().out
    assert "UTC+08:00" in text and "aaaaaaaa" in text and "proj-one" in text and "Bash" in text and "model-b" in text


def test_main_default_root_follows_claude_config_dir(env, tmp_path, monkeypatch, capsys):
    fill(env)
    other = tmp_path / "other-config"
    (other / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    assert t.default_projects_dir() == other / "projects"
    run_main(["--json", "--tz", "UTC"])
    assert json.loads(capsys.readouterr().out)["window"]["messages"] == 0          # the other folder is empty
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert t.default_projects_dir() == Path.home() / ".claude" / "projects"


def test_main_root_option(env, tmp_path, capsys):
    write_log(tmp_path / "elsewhere" / "p" / "a.jsonl", [arow("e1", "2026-03-04T10:00:00Z")])
    assert run_main(["--root", str(tmp_path / "elsewhere"), "--tz", "UTC", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["window"]["messages"] == 1


def test_main_missing_projects_folder_exits_1(env, tmp_path, capsys):
    assert run_main(["--root", str(tmp_path / "missing")]) == 1
    assert "No projects folder" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--tz", "Mars/Base"], ["--reset-day", "monday"], ["--reset-day", "x", "--reset-hour", "1"],
                                  ["--reset-day", "mon", "--reset-hour", "25"], ["--days", "0"], ["--days", "-3"], ["--top", "0"], ["--bogus"]])
def test_main_bad_settings_exit_2_with_a_message(env, capsys, argv):
    assert run_main(argv) == 2
    assert capsys.readouterr().err.strip() != ""


def test_main_bad_settings_file_exits_2(env, capsys):
    (env["logs"] / t.CONFIG_NAME).write_text("nope", encoding="utf-8")
    assert run_main([]) == 2
    assert "usage_config.json" in capsys.readouterr().err


def test_main_empty_projects_folder_reports_zero_without_crashing(env, capsys):
    assert run_main(["--tz", "UTC"]) == 0
    text = capsys.readouterr().out
    assert "0 messages" in text and "TOTAL" in text


def test_main_help_exits_zero(env, capsys):
    assert t.main(["--help"]) == 0
    assert "--reset-day" in capsys.readouterr().out


def test_main_survives_non_ascii_names(env, capsys):
    write_log(env["projects"] / "projet-été" / "s.jsonl", [arow("n1", "2026-03-04T10:00:00Z", model="modèle")])
    assert run_main(["--tz", "UTC"]) == 0


# ---------------------------------------------------------------- as a real process

def test_the_script_runs_as_a_process_with_its_own_environment(tmp_path):
    cfg = tmp_path / "cfg"
    write_log(cfg / "projects" / "p" / "s.jsonl", [arow("pr1", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), usage=(3, 0, 0, 4))])
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"),
               CLAUDE_CONFIG_DIR=str(cfg), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-B", str(HERE / "token_usage_report.py"), "--json", "--tz", "UTC"],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["window"]["total"] == 7 and out["checks"]["consistent"] is True


def test_the_source_holds_no_fixed_zone_or_reset():
    src = (HERE / "token_usage_report.py").read_text(encoding="utf-8")
    assert "UTC+8" not in src and "timedelta(hours=8)" not in src and "SG =" not in src
