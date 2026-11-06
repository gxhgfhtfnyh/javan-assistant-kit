# -*- coding: utf-8 -*-
"""Tests for status_ping.py: the pure rule (every boundary), the focus line, presence, the heartbeat plumbing, the alert, the kill switch and the hook mode."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import heartbeat  # noqa: E402
import status_ping as sp  # noqa: E402

NOW = 1_000_000.0
MIN = 60.0


def args(**over):
    a = dict(now=NOW, prompt_at=NOW - 20 * MIN, stop_at=NOW - 60 * MIN, last_ping=NOW - 60 * MIN, mode="home")
    a.update(over)
    return a


def test_all_conditions_true_sends():
    send, why = sp.decide(**args())
    assert send is True and "20 min" in why


def test_a_turn_open_exactly_the_silence_limit_does_not_ping_but_one_second_more_does():
    assert sp.decide(**args(prompt_at=NOW - 12 * MIN))[0] is False
    assert sp.decide(**args(prompt_at=NOW - 12 * MIN - 1))[0] is True


def test_a_turn_open_too_long_does_not_ping():
    assert sp.decide(**args(prompt_at=NOW - 3 * 3600 + 1, stop_at=None))[0] is True
    assert sp.decide(**args(prompt_at=NOW - 3 * 3600, stop_at=None))[0] is False
    assert "probably gone" in sp.decide(**args(prompt_at=NOW - 5 * 3600, stop_at=None))[1]


def test_the_gap_between_pings_has_a_strict_boundary():
    assert sp.decide(**args(last_ping=NOW - 15 * MIN))[0] is False
    assert sp.decide(**args(last_ping=NOW - 15 * MIN - 1))[0] is True
    assert sp.decide(**args(last_ping=None))[0] is True and sp.decide(**args(last_ping=0))[0] is True


def test_a_finished_turn_never_pings():
    assert sp.decide(**args(stop_at=NOW - 10 * MIN))[0] is False                # the assistant finished after the user wrote
    assert sp.decide(**args(stop_at=NOW - 20 * MIN))[0] is False                # same instant
    assert sp.decide(**args(stop_at=None))[0] is True                           # never finished a turn: open


def test_no_recorded_user_message_means_no_ping():
    send, why = sp.decide(**args(prompt_at=None))
    assert send is False and "no user message" in why


@pytest.mark.parametrize("mode", ["away", "asleep"])
def test_only_home_pings(mode):
    send, why = sp.decide(**args(mode=mode))
    assert send is False and mode in why


def test_decide_returns_real_bools():
    assert isinstance(sp.decide(**args())[0], bool) and isinstance(sp.decide(**args(prompt_at=None))[0], bool)


def test_presence_defaults_to_home_and_ignores_garbage(tmp_path):
    assert sp.presence_mode(tmp_path) == "home"
    (tmp_path / "presence_mode.txt").write_text("Asleep\n", encoding="utf-8")
    assert sp.presence_mode(tmp_path) == "asleep"
    (tmp_path / "presence_mode.txt").write_text("banana", encoding="utf-8")
    assert sp.presence_mode(tmp_path) == "home"


def test_focus_line_prefers_current_and_skips_rules_and_headings(tmp_path):
    (tmp_path / "focus.md").write_text("# Focus\nRULE: be careful\nCURRENT:   fixing   the  report \nother\n", encoding="utf-8")
    assert sp.focus_line(tmp_path) == "fixing the report"
    (tmp_path / "focus.md").write_text("# Focus\n---\nRULE: x\n\nwriting the plan\nsecond line\n", encoding="utf-8")
    assert sp.focus_line(tmp_path) == "writing the plan"


def test_focus_line_is_capped_and_missing_is_empty(tmp_path):
    assert sp.focus_line(tmp_path) == ""
    (tmp_path / "focus.md").write_text("CURRENT: " + "x" * 500, encoding="utf-8")
    assert len(sp.focus_line(tmp_path)) == sp.FOCUS_CAP


def test_make_text_states_facts_and_has_a_fallback():
    assert sp.make_text("fixing the report", 25) == "still working: fixing the report (no reply yet, 25 min since your last message)"
    assert "no summary set" in sp.make_text("", 13)


def write_beats(logs, prompt_ago, stop_ago, now=NOW):
    d = Path(logs) / "heartbeats"
    if prompt_ago is not None:
        heartbeat.touch("user_prompt", d, now - prompt_ago)
    if stop_ago is not None:
        heartbeat.touch("claude_session", d, now - stop_ago)


def test_run_sends_through_the_alert_and_remembers_it(tmp_path):
    write_beats(tmp_path, 20 * MIN, 90 * MIN)
    (tmp_path / "focus.md").write_text("CURRENT: fixing the report\n", encoding="utf-8")
    sent = []
    ok, msg = sp.run(logs=tmp_path, now=NOW, alert=lambda *a, **k: sent.append((a, k)))
    assert ok is True and msg.startswith("sent: still working: fixing the report")
    assert sent[0][1]["source"] == "status_ping" and sent[0][1]["toast"] is True and sent[0][1]["logs"] == str(tmp_path)
    assert json.loads((tmp_path / "status_ping_state.json").read_text(encoding="utf-8"))["t"] == NOW
    ok2, msg2 = sp.run(logs=tmp_path, now=NOW + 5 * MIN, alert=lambda *a, **k: sent.append(1))
    assert ok2 is False and "last ping" in msg2 and len(sent) == 1               # not again inside the gap
    ok3, _ = sp.run(logs=tmp_path, now=NOW + 16 * MIN, alert=lambda *a, **k: sent.append(1))
    assert ok3 is True and len(sent) == 2


def test_run_writes_a_real_alert_line_with_the_kits_notify(tmp_path):
    write_beats(tmp_path, 30 * MIN, None)
    ok, _ = sp.run(logs=tmp_path, now=NOW)
    assert ok is True
    line = (tmp_path / "alerts.txt").read_text(encoding="utf-8")
    assert "status_ping: still working" in line and line.count("\n") == 1


def test_dry_run_sends_and_saves_nothing(tmp_path):
    write_beats(tmp_path, 20 * MIN, None)
    called = []
    ok, msg = sp.run(logs=tmp_path, now=NOW, dry=True, alert=lambda *a, **k: called.append(1))
    assert ok is False and msg.startswith("WOULD SEND: still working") and not called
    assert not (tmp_path / "status_ping_state.json").exists() and not (tmp_path / "alerts.txt").exists()


def test_the_kill_switch_stops_it(tmp_path):
    write_beats(tmp_path, 20 * MIN, None)
    (tmp_path / "status_ping_off.txt").write_text("off", encoding="utf-8")
    ok, msg = sp.run(logs=tmp_path, now=NOW, alert=lambda *a, **k: pytest.fail("must not send"))
    assert ok is False and msg == "off"


def test_away_or_asleep_stops_it(tmp_path):
    write_beats(tmp_path, 20 * MIN, None)
    (tmp_path / "presence_mode.txt").write_text("asleep", encoding="utf-8")
    assert sp.run(logs=tmp_path, now=NOW, alert=lambda *a, **k: pytest.fail("must not send"))[0] is False


def test_without_heartbeats_it_says_why_and_stays_quiet(tmp_path):
    ok, msg = sp.run(logs=tmp_path, now=NOW, alert=lambda *a, **k: pytest.fail("must not send"))
    assert ok is False and "no user message" in msg


def test_a_corrupt_state_file_does_not_break_it(tmp_path):
    write_beats(tmp_path, 20 * MIN, None)
    (tmp_path / "status_ping_state.json").write_text("{bad", encoding="utf-8")
    assert sp.run(logs=tmp_path, now=NOW, alert=lambda *a, **k: None)[0] is True
    (tmp_path / "status_ping_state.json").write_text(json.dumps({"t": "yesterday"}), encoding="utf-8")
    assert sp.run(logs=tmp_path, now=NOW + 1, alert=lambda *a, **k: None)[0] is True


def test_a_failing_alert_is_reported_not_raised(tmp_path):
    write_beats(tmp_path, 20 * MIN, None)

    def boom(*a, **k):
        raise OSError("disk full")
    ok, msg = sp.run(logs=tmp_path, now=NOW, alert=boom)
    assert ok is False and "could not send (OSError)" in msg and not (tmp_path / "status_ping_state.json").exists()


def test_touch_user_writes_the_beat_and_beat_time_reads_it_back(tmp_path):
    assert sp.beat_time("user_prompt", tmp_path, NOW) is None
    assert sp.touch_user(tmp_path, now=NOW - 100) is True
    assert sp.beat_time("user_prompt", tmp_path, NOW) == NOW - 100


def test_touch_user_never_raises(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file", encoding="utf-8")
    assert sp.touch_user(blocker / "logs") is False


def test_the_minutes_option_parses_and_falls_back():
    assert sp._minutes(["--gap-min", "2"], "--gap-min", 99) == 120
    assert sp._minutes(["--gap-min", "soon"], "--gap-min", 99) == 99
    assert sp._minutes(["--gap-min"], "--gap-min", 99) == 99
    assert sp._minutes([], "--gap-min", 99) == 99


def test_main_prints_the_decision(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sp, "LOGS", tmp_path)
    monkeypatch.setattr(sys, "stdin", None)
    assert sp.main(["--dry-run"]) == 0
    assert "no ping" in capsys.readouterr().out


def run_cli(tmp_path, *args, stdin=b""):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, str(HERE / "status_ping.py"), *args], input=stdin, capture_output=True, env=env, timeout=30)


def test_real_process_as_a_prompt_hook_records_the_prompt_silently(tmp_path):
    payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "do the thing"}).encode("utf-8")
    r = run_cli(tmp_path, stdin=payload)
    assert r.returncode == 0 and r.stdout == b""
    assert (tmp_path / "logs" / "heartbeats" / "user_prompt.beat").exists()


def test_real_process_as_another_hook_does_not_run_the_job_or_print(tmp_path):
    r = run_cli(tmp_path, stdin=json.dumps({"hook_event_name": "Stop"}).encode("utf-8"))
    assert r.returncode == 0 and r.stdout == b"" and not (tmp_path / "logs" / "heartbeats" / "user_prompt.beat").exists()


def test_real_process_as_a_job_with_empty_stdin_prints_the_decision(tmp_path):
    r = run_cli(tmp_path, stdin=b"")
    assert r.returncode == 0 and b"no ping" in r.stdout


def test_real_process_touch_user_flag(tmp_path):
    r = run_cli(tmp_path, "--touch-user")
    assert r.returncode == 0 and (tmp_path / "logs" / "heartbeats" / "user_prompt.beat").exists()


def test_real_process_sends_a_ping_when_a_turn_has_been_open_long(tmp_path):
    logs = tmp_path / "logs"
    import time
    heartbeat.touch("user_prompt", logs / "heartbeats", time.time() - 30 * 60)
    (logs / "focus.md").write_text("CURRENT: writing the summary\n", encoding="utf-8")
    r = run_cli(tmp_path, stdin=b"")
    assert r.returncode == 0 and b"sent: still working: writing the summary" in r.stdout
    assert "status_ping: still working" in (logs / "alerts.txt").read_text(encoding="utf-8")
