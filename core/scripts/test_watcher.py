# -*- coding: utf-8 -*-
"""Tests for watcher.py: config, tail cursors, command sources, sanitising, health lines, the filter hook, the lock, the loop and the real process."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import watcher as w  # noqa: E402


def collect(sources, state, now, logs, root=None, runner=None):
    out = []
    kwargs = {"runner": runner} if runner else {}
    w.poll_once(sources, state, now, out.append, logs=logs, root=root, **kwargs)
    return out


def tail(name="t", path="t.txt", **extra):
    d = {"name": name, "type": "tail", "path": path, "tag": "T"}
    d.update(extra)
    return d


def append(path, text):
    with open(path, "ab") as f:
        f.write(text.encode("utf-8"))


def fake_runner(stdout="", code=0, calls=None, exc=None):
    def run(argv, **kw):
        if calls is not None:
            calls.append((argv, kw))
        if exc:
            raise exc
        return SimpleNamespace(returncode=code, stdout=stdout.encode("utf-8"), stderr=b"")
    return run


# ---------------------------------------------------------------- clean
def test_clean_makes_one_printable_line():
    s = w.clean("a\r\nb\tc\x00d\x1b[31me" + chr(0x202e) + chr(0x200b) + chr(0x2028) + "f   g")
    assert s == "a b cd[31me f g" and "\n" not in s
    assert w.clean("x" * 1000) == "x" * w.MAX_LINE
    assert w.clean(None) == "None" and w.clean("") == ""


# ---------------------------------------------------------------- config
def test_no_config_file_means_the_alerts_source(tmp_path):
    poll, sources, problems = w.load_config(tmp_path)
    assert [s["name"] for s in sources] == ["alerts"] and problems == [] and poll == w.DEFAULT_POLL_S


def test_a_config_that_does_not_parse_falls_back_with_a_problem(tmp_path):
    (tmp_path / "watcher_sources.json").write_text("{not json", encoding="utf-8")
    _poll, sources, problems = w.load_config(tmp_path)
    assert [s["name"] for s in sources] == ["alerts"] and len(problems) == 1


def test_a_config_with_a_byte_order_mark_still_loads(tmp_path):
    (tmp_path / "watcher_sources.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"sources": [tail("a")]}).encode("utf-8"))
    assert [s["name"] for s in w.load_config(tmp_path)[1]] == ["a"]


def test_bad_sources_are_dropped_and_named():
    data = {"poll_s": 0.01, "sources": [
        tail("ok"), tail("ok"), {"name": "bad name!", "type": "tail", "path": "x"}, {"name": "nopath", "type": "tail"},
        {"name": "cmd", "type": "command", "argv": []}, {"name": "cmd2", "type": "command", "argv": ["x", 5]},
        {"name": "odd", "type": "udp"}, "string", {"name": "f", "type": "tail", "path": "x", "format": "xml"},
        {"name": "good", "type": "command", "argv": ["python", "x.py"]}]}
    poll, sources, problems = w.parse_config(data)
    assert [s["name"] for s in sources] == ["ok", "good"] and len(problems) == 8
    assert poll == w.MIN_POLL_S                                                # never a busy loop


def test_an_explicit_empty_source_list_means_no_sources_and_a_non_list_is_a_problem():
    assert w.parse_config({"sources": []})[1] == []
    assert w.parse_config({"sources": "x"})[2] and w.parse_config([1])[2]
    assert w.parse_config({"poll_s": "fast"})[0] == w.DEFAULT_POLL_S


def test_the_tag_is_cleaned_and_defaults_to_the_name():
    assert w.tag_of({"name": "mail", "tag": "In-Box! [x]"}) == "IN-BOXX"
    assert w.tag_of({"name": "mail"}) == "MAIL" and w.tag_of({"name": "m", "tag": "!!!"}) == "WATCH"


# ---------------------------------------------------------------- tail sources
def test_a_new_source_starts_at_the_end_and_then_reads_what_is_appended(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("old one\nold two\n", encoding="utf-8")
    state = {}
    assert collect([tail()], state, 0, tmp_path) == []
    append(f, "new one\nnew two\n")
    assert collect([tail()], state, 1, tmp_path) == ["[T] new one", "[T] new two"]
    assert collect([tail()], state, 2, tmp_path) == []                         # never twice


def test_from_start_reads_the_whole_file_once(tmp_path):
    (tmp_path / "t.txt").write_text("a\nb\n", encoding="utf-8")
    state = {}
    assert collect([tail(from_start=True)], state, 0, tmp_path) == ["[T] a", "[T] b"]
    assert collect([tail(from_start=True)], state, 1, tmp_path) == []


def test_a_half_written_line_waits_for_its_newline(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("", encoding="utf-8")
    state = {}
    collect([tail()], state, 0, tmp_path)
    append(f, "par")
    assert collect([tail()], state, 1, tmp_path) == []
    append(f, "tial\nnext\n")
    assert collect([tail()], state, 2, tmp_path) == ["[T] partial", "[T] next"]


def test_a_missing_file_is_quiet_and_is_read_from_the_top_when_it_appears(tmp_path):
    state = {}
    assert collect([tail()], state, 0, tmp_path) == [] and not state.get("errors")
    (tmp_path / "t.txt").write_text("first\n", encoding="utf-8")
    assert collect([tail()], state, 1, tmp_path) == ["[T] first"]


def test_a_truncated_or_replaced_file_is_read_again_from_the_top(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("aaaaaaaaaa\nbbbbbbbbbb\n", encoding="utf-8")
    state = {}
    collect([tail()], state, 0, tmp_path)
    f.write_text("new\n", encoding="utf-8")                                    # shorter than the cursor
    assert collect([tail()], state, 1, tmp_path) == ["[T] new"]
    state["tail"]["t"]["ino"] = 123456789                                      # pretend the file was replaced by another one
    append(f, "more\n")
    assert collect([tail()], state, 2, tmp_path) == ["[T] new", "[T] more"]


def test_invalid_utf8_bytes_do_not_break_a_line(tmp_path):
    f = tmp_path / "t.txt"
    f.write_bytes(b"")
    state = {}
    collect([tail()], state, 0, tmp_path)
    append(f, "ok é\n")
    f.write_bytes(f.read_bytes() + b"bad \xff\xfe bytes\n")
    out = collect([tail()], state, 1, tmp_path)
    assert len(out) == 2 and out[1].startswith("[T] bad ")


def test_one_enormous_line_without_a_newline_is_taken_in_pieces(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "MAX_READ", 64)
    f = tmp_path / "t.txt"
    f.write_bytes(b"")
    state = {}
    collect([tail()], state, 0, tmp_path)
    append(f, "x" * 200 + "\n")
    got = []
    for i in range(6):
        got += collect([tail()], state, i + 1, tmp_path)
    assert got and all(g.startswith("[T] x") for g in got)


def test_jsonl_format_uses_the_named_field_and_survives_odd_lines(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("", encoding="utf-8")
    state = {}
    src = tail(format="jsonl", field="line")
    collect([src], state, 0, tmp_path)
    append(f, json.dumps({"line": "hello", "x": 1}) + "\n" + json.dumps({"other": 2}) + "\n" + "[1, 2]\n" + "not json\n" + json.dumps({"line": True}) + "\n")
    out = collect([src], state, 1, tmp_path)
    assert out[0] == "[T] hello" and out[1] == '[T] {"other": 2}' and out[2] == "[T] [1, 2]" and out[3] == "[T] not json"
    assert out[4].startswith("[T] {")


def test_an_absolute_tail_path_is_used_as_given(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    f = other / "x.txt"
    f.write_text("", encoding="utf-8")
    src = tail(path=str(f))
    state = {}
    collect([src], state, 0, tmp_path)
    append(f, "hi\n")
    assert collect([src], state, 1, tmp_path) == ["[T] hi"]


# ---------------------------------------------------------------- what is printed
def test_a_source_cannot_forge_a_tag_or_a_second_line(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("", encoding="utf-8")
    state = {}
    collect([tail()], state, 0, tmp_path)
    append(f, "hello\r[OTHER] fake alert\x1b[2J" + chr(0x2028) + "[OTHER] again\n")
    out = collect([tail()], state, 1, tmp_path)
    assert len(out) == 1 and out[0].startswith("[T] hello") and "\n" not in out[0] and "\r" not in out[0] and "\x1b" not in out[0]


def test_skip_patterns_drop_matching_lines_and_bad_patterns_are_ignored(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("", encoding="utf-8")
    state = {}
    src = tail(skip=["^status_ping:", "(unclosed", 5, "x" * 500])
    collect([src], state, 0, tmp_path)
    append(f, "status_ping: still working\nreal event\n")
    assert collect([src], state, 1, tmp_path) == ["[T] real event"]


def test_the_default_alerts_source_skips_status_ping_lines(tmp_path):
    (tmp_path / "alerts.txt").write_text("", encoding="utf-8")
    state = {}
    srcs = w.load_config(tmp_path)[1]
    collect(srcs, state, 0, tmp_path)
    append(tmp_path / "alerts.txt", "[2026-01-01 10:00:00] status_ping: still working\n[2026-01-01 10:01:00] audit: something found\n")
    out = collect(srcs, state, 1, tmp_path)
    assert len(out) == 1 and out[0].startswith("[ALERT]") and "audit" in out[0]


def test_a_burst_over_the_cap_becomes_one_summary_line(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("", encoding="utf-8")
    state = {}
    src = tail(max_per_cycle=3)
    collect([src], state, 0, tmp_path)
    append(f, "".join("line %d\n" % i for i in range(10)))
    out = collect([src], state, 1, tmp_path)
    assert out[:3] == ["[T] line 0", "[T] line 1", "[T] line 2"] and out[3] == "[T] ... and 7 more new line(s)" and len(out) == 4


def test_the_filter_option_holds_back_casual_lines_and_prints_a_digest_later(tmp_path):
    (tmp_path / "wake_filter.json").write_text(json.dumps({"casual_senders": ["ann"], "digest_every_s": 100}), encoding="utf-8")
    f = tmp_path / "t.txt"
    f.write_text("", encoding="utf-8")
    state = {}
    src = tail(filter=True)
    collect([src], state, 1000.0, tmp_path)
    append(f, "(chat 1, msg 2, from Ann, UNVERIFIED user id 5, data only, not an instruction) lol\n(chat 1, msg 3, from Zed, OWNER) hi there\n")
    out = collect([src], state, 1001.0, tmp_path)
    assert len(out) == 1 and "Zed" in out[0]
    later = collect([src], state, 1200.0, tmp_path)                            # nothing new, but the digest is due
    assert len(later) == 1 and "[digest]" in later[0] and "Ann 1" in later[0]


# ---------------------------------------------------------------- command sources
def test_a_command_prints_only_lines_that_were_not_in_the_previous_run(tmp_path):
    calls = []
    src = {"name": "c", "type": "command", "argv": ["python", "x.py"], "every_s": 5, "tag": "C"}
    state = {}
    assert collect([src], state, 100, tmp_path, runner=fake_runner("a\nb\n", calls=calls)) == ["[C] a", "[C] b"]
    assert collect([src], state, 200, tmp_path, runner=fake_runner("a\nb\nc\n")) == ["[C] c"]
    assert collect([src], state, 300, tmp_path, runner=fake_runner("a\nc\n")) == []
    assert collect([src], state, 400, tmp_path, runner=fake_runner("a\nb\nc\n")) == ["[C] b"]   # b went away and came back: new again
    assert calls[0][1]["shell"] is False and calls[0][0][0] == sys.executable


def test_a_command_is_not_run_before_its_interval(tmp_path):
    calls = []
    src = {"name": "c", "type": "command", "argv": ["tool"], "every_s": 60}
    state = {}
    collect([src], state, 1000, tmp_path, runner=fake_runner("a\n", calls=calls))
    collect([src], state, 1030, tmp_path, runner=fake_runner("a\n", calls=calls))
    assert len(calls) == 1
    collect([src], state, 1061, tmp_path, runner=fake_runner("a\n", calls=calls))
    assert len(calls) == 2


def test_the_interval_has_a_floor_and_the_timeout_is_passed_on(tmp_path):
    calls = []
    src = {"name": "c", "type": "command", "argv": ["tool"], "every_s": 0.001, "timeout_s": 7}
    state = {}
    collect([src], state, 1000, tmp_path, runner=fake_runner("", calls=calls))
    collect([src], state, 1001, tmp_path, runner=fake_runner("", calls=calls))
    assert len(calls) == 1 and calls[0][1]["timeout"] == 7


def test_announce_existing_false_makes_the_first_run_a_silent_baseline(tmp_path):
    src = {"name": "c", "type": "command", "argv": ["tool"], "every_s": 5, "announce_existing": False}
    state = {}
    assert collect([src], state, 100, tmp_path, runner=fake_runner("a\nb\n")) == []
    assert collect([src], state, 200, tmp_path, runner=fake_runner("a\nb\nc\n")) == ["[C] c"]
    assert state["cmd"]["c"]["seen"]


def test_duplicate_lines_in_one_run_print_once(tmp_path):
    src = {"name": "c", "type": "command", "argv": ["tool"], "every_s": 5, "tag": "C"}
    assert collect([src], {}, 100, tmp_path, runner=fake_runner("a\na\nb\n")) == ["[C] a", "[C] b"]


def test_a_failing_command_reports_once_then_recovers(tmp_path):
    src = {"name": "c", "type": "command", "argv": ["tool"], "every_s": 5, "tag": "C"}
    state = {}
    first = collect([src], state, 100, tmp_path, runner=fake_runner("", code=2))
    assert first == ["[watcher] source c problem: exit code 2"]
    assert collect([src], state, 200, tmp_path, runner=fake_runner("", code=2)) == []              # not repeated every cycle
    back = collect([src], state, 300, tmp_path, runner=fake_runner("fine\n"))
    assert back == ["[watcher] source c recovered", "[C] fine"]


def test_timeouts_and_start_failures_are_problems_not_crashes(tmp_path):
    src = {"name": "c", "type": "command", "argv": ["tool"], "every_s": 5}
    out = collect([src], {}, 100, tmp_path, runner=fake_runner(exc=subprocess.TimeoutExpired("tool", 3)))
    assert out and "timed out" in out[0]
    out = collect([src], {}, 100, tmp_path, runner=fake_runner(exc=FileNotFoundError("nope")))
    assert out and "could not start (FileNotFoundError)" in out[0]


def test_one_broken_source_does_not_stop_the_next(tmp_path):
    (tmp_path / "ok.txt").write_text("", encoding="utf-8")
    bad = {"name": "bad", "type": "command", "argv": ["tool"], "every_s": 5}
    good = tail("good", "ok.txt")
    state = {}
    first = collect([bad, good], state, 100, tmp_path, runner=fake_runner(exc=RuntimeError("x")))
    assert first == ["[watcher] source bad problem: could not start (RuntimeError)"]
    append(tmp_path / "ok.txt", "hello\n")
    out = collect([bad, good], state, 200, tmp_path, runner=fake_runner(exc=RuntimeError("x")))
    assert out == ["[T] hello"]


def test_resolve_argv_maps_python_and_project_relative_scripts(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "x.py").write_text("", encoding="utf-8")
    argv = w.resolve_argv(["python", "scripts/x.py", "--flag", "scripts/missing.py"], tmp_path)
    assert argv[0] == sys.executable and argv[1] == str(tmp_path / "scripts" / "x.py") and argv[2] == "--flag" and argv[3] == "scripts/missing.py"
    assert w.resolve_argv(["other-tool", "a"], tmp_path) == ["other-tool", "a"]


def test_command_output_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "MAX_COMMAND_OUTPUT", 20)
    src = {"name": "c", "type": "command", "argv": ["tool"], "every_s": 5, "tag": "C"}
    out = collect([src], {}, 100, tmp_path, runner=fake_runner("0123456789\n" * 50))
    assert 1 <= len(out) <= 2


# ---------------------------------------------------------------- lock
def test_only_one_watcher_holds_the_lock(tmp_path):
    assert w.acquire_lock(tmp_path, pid=111) is True
    assert w.acquire_lock(tmp_path, pid=222) is False
    assert w.acquire_lock(tmp_path, pid=111) is True                           # the owner may take it again


def test_a_lock_nobody_refreshed_is_taken_over(tmp_path):
    assert w.acquire_lock(tmp_path, pid=111) is True
    old = time.time() - w.LOCK_STALE_S - 5
    os.utime(tmp_path / "watcher.lock", (old, old))
    assert w.acquire_lock(tmp_path, pid=222) is True
    assert (tmp_path / "watcher.lock").read_text(encoding="utf-8") == "222"


def test_release_removes_only_our_own_lock(tmp_path):
    w.acquire_lock(tmp_path, pid=111)
    w.release_lock(tmp_path, pid=222)
    assert (tmp_path / "watcher.lock").exists()
    w.release_lock(tmp_path, pid=111)
    assert not (tmp_path / "watcher.lock").exists()


def test_an_unwritable_lock_folder_does_not_stop_the_watcher(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    assert w.acquire_lock(blocker / "logs", pid=1) is True


# ---------------------------------------------------------------- the loop
def loop(tmp_path, cycles, config=None, runner=None, clock=None):
    if config is not None:
        (tmp_path / "watcher_sources.json").write_text(json.dumps(config), encoding="utf-8")
    out = []
    ticks = iter(range(1000, 100000, 10))
    code = w.run(logs=tmp_path, root=tmp_path, max_cycles=cycles, emit=out.append, sleep=lambda s: None, clock=clock or (lambda: next(ticks)),
                 **({"runner": runner} if runner else {}))
    assert code == 0
    return out


def test_the_loop_prints_new_events_saves_the_cursor_and_does_not_repeat_after_a_restart(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("", encoding="utf-8")
    cfg = {"sources": [tail()]}
    loop(tmp_path, 1, cfg)                                                      # first run records the end of the file
    append(f, "one\ntwo\n")
    assert loop(tmp_path, 1) == ["[T] one", "[T] two"]
    assert loop(tmp_path, 3) == []                                              # a restart does not replay them
    assert json.loads((tmp_path / "watcher_state.json").read_text(encoding="utf-8"))["tail"]["t"]["offset"] == f.stat().st_size


def test_the_loop_writes_a_heartbeat_and_releases_its_lock(tmp_path):
    loop(tmp_path, 2, {"sources": []})
    assert (tmp_path / "heartbeats" / "watcher.beat").exists() and not (tmp_path / "watcher.lock").exists()


def test_the_kill_switch_makes_the_loop_idle(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("", encoding="utf-8")
    loop(tmp_path, 1, {"sources": [tail()]})
    append(f, "event\n")
    (tmp_path / "watcher_off.txt").write_text("off", encoding="utf-8")
    assert loop(tmp_path, 3) == []
    (tmp_path / "watcher_off.txt").unlink()
    assert loop(tmp_path, 1) == ["[T] event"]                                   # nothing was lost while it was off


def test_a_second_watcher_says_so_and_exits(tmp_path):
    w.acquire_lock(tmp_path, pid=999999)
    out = []
    assert w.run(logs=tmp_path, root=tmp_path, max_cycles=1, emit=out.append, sleep=lambda s: None) == 0
    assert out and "already running" in out[0]


def test_config_problems_are_printed_at_start(tmp_path):
    out = loop(tmp_path, 1, {"sources": [{"name": "x", "type": "udp"}]})
    assert out and "source x ignored" in out[0]


def test_a_changed_config_is_picked_up_without_a_restart(tmp_path):
    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    (tmp_path / "b.txt").write_text("", encoding="utf-8")
    cfgfile = tmp_path / "watcher_sources.json"
    cfgfile.write_text(json.dumps({"sources": [tail("a", "a.txt")]}), encoding="utf-8")
    out = []
    n = {"i": 0}

    def sleep(_s):
        n["i"] += 1
        if n["i"] == 1:
            append(tmp_path / "a.txt", "from a\n")
            cfgfile.write_text(json.dumps({"sources": [tail("a", "a.txt"), tail("b", "b.txt", tag="B")]}), encoding="utf-8")
            os.utime(cfgfile, (time.time() + 5, time.time() + 5))
        if n["i"] == 2:
            append(tmp_path / "b.txt", "from b\n")
    ticks = iter(range(1000, 100000, 10))
    w.run(logs=tmp_path, root=tmp_path, max_cycles=4, emit=out.append, sleep=sleep, clock=lambda: next(ticks))
    assert "[T] from a" in out
    assert "[B] from b" in out                                                  # the new source was added while running and started at the end of its file


def test_a_corrupt_state_file_means_a_fresh_start(tmp_path):
    (tmp_path / "watcher_state.json").write_text("{broken", encoding="utf-8")
    assert w.load_state(tmp_path) == {}
    (tmp_path / "watcher_state.json").write_text("[1]", encoding="utf-8")
    assert w.load_state(tmp_path) == {}


def test_an_unwritable_state_folder_is_survived(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    assert w.save_state({"a": 1}, blocker / "logs") is False


def test_module_logs_default_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "LOGS", tmp_path)
    (tmp_path / "t.txt").write_text("", encoding="utf-8")
    (tmp_path / "watcher_sources.json").write_text(json.dumps({"sources": [tail()]}), encoding="utf-8")
    out = []
    w.run(max_cycles=1, emit=out.append, sleep=lambda s: None)
    assert (tmp_path / "watcher_state.json").exists()


# ---------------------------------------------------------------- the real process
def run_cli(tmp_path, *args, timeout=30):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, str(HERE / "watcher.py"), *args], capture_output=True, env=env, timeout=timeout)


def test_real_process_once_prints_events_from_a_tail_and_a_command(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "t.txt").write_text("history\n", encoding="utf-8")
    cmd = [sys.executable, "-c", "print('from command'); print('caf' + chr(233))"]
    (logs / "watcher_sources.json").write_text(json.dumps({"sources": [tail(), {"name": "c", "type": "command", "argv": cmd, "tag": "C"}]}), encoding="utf-8")
    r = run_cli(tmp_path, "--once")
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "[C] from command" in out and "[C] caf" in out and "history" not in out
    with open(logs / "t.txt", "ab") as f:
        f.write("new event\n".encode("utf-8"))
    r2 = run_cli(tmp_path, "--once")
    out2 = r2.stdout.decode("utf-8")
    assert "[T] new event" in out2 and "from command" not in out2               # the cursor was saved, and the command is not due again yet


def test_real_process_list_shows_sources_and_problems(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "watcher_sources.json").write_text(json.dumps({"sources": [tail("a"), {"name": "bad", "type": "x"}]}), encoding="utf-8")
    r = run_cli(tmp_path, "--list")
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "1 source(s)" in out and "PROBLEM" in out and "[T]" in out


def test_real_process_with_a_bad_interval_exits_2(tmp_path):
    r = run_cli(tmp_path, "--interval", "soon")
    assert r.returncode == 2
