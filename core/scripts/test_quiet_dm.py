# -*- coding: utf-8 -*-
"""Tests for quiet_dm: when a message is held, the held queue, merging and chunking, delivery and its failure handling, the sender choice and the command line."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import quiet_dm as qd  # noqa: E402


class Sender:
    """A fake delivery route: records each text, answers with the next result in `results` (default True)."""
    def __init__(self, *results):
        self.texts = []
        self.results = list(results)

    def __call__(self, text):
        self.texts.append(text)
        return self.results.pop(0) if self.results else True


def queue_rows(tmp_path):
    p = tmp_path / qd.QUEUE_NAME
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def put(tmp_path, *items):
    """Write held rows: items are (ts, text) pairs."""
    qd._write([{"ts": ts, "source": "t", "text": text} for ts, text in items], tmp_path)


# ---------- holding and presence ----------
def test_holding_only_while_asleep_and_not_switched_off_or_overridden():
    assert qd.holding("asleep", False, {}) is True
    assert not qd.holding("home", False, {}) and not qd.holding("away", False, {})
    assert not qd.holding("asleep", True, {})
    assert not qd.holding("asleep", False, {"QUIET_DM_NOW": "1"})


def test_holding_reads_the_real_environment_by_default(monkeypatch):
    monkeypatch.delenv("QUIET_DM_NOW", raising=False)
    assert qd.holding("asleep") is True
    monkeypatch.setenv("QUIET_DM_NOW", "1")
    assert not qd.holding("asleep")


def test_current_mode_reads_the_presence_file_and_defaults_to_home(tmp_path):
    assert qd.current_mode(tmp_path) == "home"
    f = tmp_path / qd.PRESENCE_NAME
    for text, want in (("asleep", "asleep"), ("  AWAY \n", "away"), ("home", "home"), ("sleeping", "home"), ("", "home")):
        f.write_text(text, encoding="utf-8")
        assert qd.current_mode(tmp_path) == want


def test_current_mode_survives_a_byte_order_mark_so_a_night_message_stays_held(tmp_path):
    (tmp_path / qd.PRESENCE_NAME).write_bytes(b"\xef\xbb\xbfasleep\r\n")
    assert qd.current_mode(tmp_path) == "asleep"


# ---------- choosing the delivery route ----------
def test_sender_argv_is_none_without_any_setup(tmp_path):
    assert qd.sender_argv(tmp_path) is None


def test_sender_argv_uses_the_discord_pack_when_it_is_set_up(tmp_path):
    (tmp_path / "discord_config.json").write_text("{}", encoding="utf-8")
    argv = qd.sender_argv(tmp_path)
    assert argv[0] == sys.executable and argv[1].endswith("discord_queue_send.py") and argv[2:] == ["--stdin", "--lint-ok"]


def test_sender_argv_prefers_the_sender_file_and_maps_python_to_this_interpreter(tmp_path):
    (tmp_path / "discord_config.json").write_text("{}", encoding="utf-8")
    (tmp_path / qd.SENDER_NAME).write_text(json.dumps({"argv": ["python", "send_it.py", "--now"]}), encoding="utf-8-sig")
    assert qd.sender_argv(tmp_path) == [sys.executable, "send_it.py", "--now"]
    (tmp_path / qd.SENDER_NAME).write_text(json.dumps({"argv": ["/usr/bin/tool", "x"]}), encoding="utf-8")
    assert qd.sender_argv(tmp_path) == ["/usr/bin/tool", "x"]


def test_sender_argv_ignores_a_bad_sender_file(tmp_path):
    f = tmp_path / qd.SENDER_NAME
    for bad in ("{ nope", json.dumps({"argv": "python x"}), json.dumps({"argv": []}), json.dumps({"argv": ["ok", ""]}), json.dumps({"argv": ["ok", 5]}), json.dumps([1])):
        f.write_text(bad, encoding="utf-8")
        assert qd.sender_argv(tmp_path) is None


def test_direct_runs_the_sender_with_the_text_on_stdin_and_reports_success(tmp_path, monkeypatch):
    (tmp_path / qd.SENDER_NAME).write_text(json.dumps({"argv": ["python", "s.py"]}), encoding="utf-8")
    calls = []

    class Done:
        def __init__(self, code):
            self.returncode = code

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return Done(0 if len(calls) == 1 else 1)
    monkeypatch.setattr(qd.subprocess, "run", fake_run)
    assert qd._direct("hello", tmp_path) is True and qd._direct("again", tmp_path) is False
    assert calls[0][0] == [sys.executable, "s.py"] and calls[0][1]["input"] == "hello" and calls[0][1]["timeout"] == 60


def test_direct_reports_failure_instead_of_raising(tmp_path, monkeypatch):
    (tmp_path / qd.SENDER_NAME).write_text(json.dumps({"argv": ["python", "s.py"]}), encoding="utf-8")

    def boom(*a, **kw):
        raise OSError("no such program")
    monkeypatch.setattr(qd.subprocess, "run", boom)
    assert qd._direct("x", tmp_path) is False


def test_direct_without_a_sender_writes_an_alert_line(tmp_path):
    assert qd._direct("night note", tmp_path) is True
    assert "night note" in (tmp_path / "alerts.txt").read_text(encoding="utf-8")


# ---------- the held queue ----------
def test_read_skips_bad_rows_and_keeps_only_the_newest_rows(tmp_path, monkeypatch):
    (tmp_path / qd.QUEUE_NAME).write_text("\n".join(["{ no", "[1]", json.dumps({"text": ""}), json.dumps({"text": 5})] + [json.dumps({"ts": i, "text": "t%d" % i}) for i in range(6)]) + "\n", encoding="utf-8")
    monkeypatch.setattr(qd, "MAX_ROWS", 4)
    assert [r["text"] for r in qd._read(tmp_path)] == ["t2", "t3", "t4", "t5"]
    assert qd._read(tmp_path / "missing") == []


def test_write_replaces_the_queue_and_removes_the_file_when_empty(tmp_path):
    put(tmp_path, (1, "a"), (2, "ünï"))
    assert [r["text"] for r in queue_rows(tmp_path)] == ["a", "ünï"]
    qd._write([], tmp_path)
    assert not (tmp_path / qd.QUEUE_NAME).exists()
    qd._write([], tmp_path)                                    # nothing there: still fine


# ---------- send ----------
def test_send_delivers_at_once_when_not_asleep_and_holds_nothing(tmp_path):
    s = Sender(True)
    assert qd.send("hi", source="x", logs=tmp_path, mode_now="home", sender=s) is True
    assert s.texts == ["hi"] and queue_rows(tmp_path) == []
    assert qd.send("hi", logs=tmp_path, mode_now="away", sender=Sender(False)) is False       # the delivery result is passed on


def test_send_holds_while_asleep_and_calls_nobody(tmp_path):
    s = Sender()
    assert qd.send("later", source="s" * 100, logs=tmp_path, mode_now="asleep", sender=s, now=500.0) is True
    assert s.texts == []
    (row,) = queue_rows(tmp_path)
    assert row["ts"] == 500.0 and row["text"] == "later" and row["source"] == "s" * 60


def test_send_cuts_an_enormous_item(tmp_path):
    qd.send("x" * (qd.MAX_ITEM + 500), logs=tmp_path, mode_now="asleep", sender=Sender())
    assert len(queue_rows(tmp_path)[0]["text"]) == qd.MAX_ITEM


def test_send_reads_the_presence_file_when_no_mode_is_given(tmp_path):
    (tmp_path / qd.PRESENCE_NAME).write_text("asleep", encoding="utf-8")
    s = Sender()
    qd.send("quiet", logs=tmp_path, sender=s)
    assert s.texts == [] and len(queue_rows(tmp_path)) == 1


def test_send_delivers_when_the_switch_file_exists_or_the_override_is_set(tmp_path, monkeypatch):
    (tmp_path / qd.OFF_NAME).write_text("off", encoding="utf-8")
    s = Sender()
    qd.send("now", logs=tmp_path, mode_now="asleep", sender=s)
    assert s.texts == ["now"] and queue_rows(tmp_path) == []
    (tmp_path / qd.OFF_NAME).unlink()
    monkeypatch.setenv("QUIET_DM_NOW", "1")
    qd.send("urgent", logs=tmp_path, mode_now="asleep", sender=s)
    assert s.texts == ["now", "urgent"]


def test_send_fails_open_when_holding_breaks_and_delivers_exactly_once(tmp_path, monkeypatch):
    def broken_lock(logs=None):
        raise RuntimeError("lock folder is not writable")
    monkeypatch.setattr(qd, "_lock", broken_lock)
    s = Sender(True)
    assert qd.send("must not be lost", logs=tmp_path, mode_now="asleep", sender=s) is True
    assert s.texts == ["must not be lost"]


def test_a_sender_that_raises_is_called_once_not_twice(tmp_path):
    calls = []

    def bad(text):
        calls.append(text)
        raise RuntimeError("route down")
    with pytest.raises(RuntimeError):
        qd.send("x", logs=tmp_path, mode_now="home", sender=bad)
    assert calls == ["x"]


# ---------- merging and chunking ----------
def test_build_messages_merges_identical_texts_with_a_count_and_keeps_the_order():
    rows = [{"text": "b"}, {"text": "a"}, {"text": "b "}, {"text": "  "}, {"text": "b"}]
    ((text, used),) = qd.build_messages(rows)
    assert text == qd.HEADER + "\n\n" + "b (x3)\n\na" and len(used) == 4


def test_build_messages_with_nothing_makes_nothing():
    assert qd.build_messages([]) == [] and qd.build_messages([{"text": " "}]) == []


def test_build_messages_splits_at_the_limit_and_loses_no_row():
    rows = [{"ts": i, "text": ("item %d " % i) + "w" * 600} for i in range(7)]
    parts = qd.build_messages(rows)
    assert len(parts) >= 3 and all(len(t) <= qd.CHUNK for t, _ in parts)
    assert sum(len(rs) for _, rs in parts) == 7
    assert [r["ts"] for _, rs in parts for r in rs] == list(range(7))


def test_build_messages_cuts_one_oversized_item():
    ((text, _),) = qd.build_messages([{"text": "z" * 5000}])
    assert len(text) <= qd.CHUNK and text.endswith("(cut)")


# ---------- flush ----------
def test_flush_with_an_empty_queue_does_nothing(tmp_path):
    s = Sender()
    assert qd.flush(tmp_path, mode_now="home", sender=s) == (0, 0) and s.texts == []


def test_flush_keeps_everything_while_still_asleep(tmp_path):
    put(tmp_path, (1000, "a"))
    s = Sender()
    assert qd.flush(tmp_path, mode_now="asleep", sender=s, now=1000 + 3600) == (0, 0)
    assert s.texts == [] and len(queue_rows(tmp_path)) == 1


def test_flush_sends_an_item_older_than_twelve_hours_even_while_asleep(tmp_path):
    put(tmp_path, (1000, "old news"))
    s = Sender()
    assert qd.flush(tmp_path, mode_now="asleep", sender=s, now=1000 + qd.STALE_S + 1) == (1, 1)
    assert "old news" in s.texts[0] and queue_rows(tmp_path) == []


def test_flush_delivers_one_merged_message_when_awake_and_empties_the_queue(tmp_path):
    put(tmp_path, (1, "x"), (2, "y"), (3, "x"))
    s = Sender()
    assert qd.flush(tmp_path, mode_now="home", sender=s, now=10) == (3, 1)
    assert len(s.texts) == 1 and "x (x2)" in s.texts[0] and "y" in s.texts[0]
    assert queue_rows(tmp_path) == []


def test_flush_keeps_the_items_when_delivery_fails_and_retries_later(tmp_path):
    put(tmp_path, (1, "a"), (2, "b"))
    assert qd.flush(tmp_path, mode_now="home", sender=Sender(False), now=10) == (0, 0)
    assert len(queue_rows(tmp_path)) == 2
    assert qd.flush(tmp_path, mode_now="home", sender=Sender(True), now=11) == (2, 1)


def test_flush_keeps_only_the_chunks_after_a_failed_one(tmp_path):
    put(tmp_path, *[(i, ("n%d " % i) + "w" * 900) for i in range(4)])
    delivered, msgs = qd.flush(tmp_path, mode_now="home", sender=Sender(True, False), now=10)
    left = queue_rows(tmp_path)
    assert msgs == 1 and 0 < delivered < 4 and len(left) == 4 - delivered
    assert [r["ts"] for r in left] == list(range(delivered, 4))


def test_flush_keeps_a_row_that_arrived_while_it_was_delivering(tmp_path):
    put(tmp_path, (1, "first"))

    def sender_that_races(text):
        with open(tmp_path / qd.QUEUE_NAME, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": 2, "source": "t", "text": "arrived meanwhile"}) + "\n")
        return True
    assert qd.flush(tmp_path, mode_now="home", sender=sender_that_races, now=10) == (1, 1)
    assert [r["text"] for r in queue_rows(tmp_path)] == ["arrived meanwhile"]


def test_flush_dry_run_prints_and_changes_nothing(tmp_path, capsys):
    put(tmp_path, (1, "a"), (2, "b"))
    s = Sender()
    assert qd.flush(tmp_path, mode_now="home", sender=s, dry_run=True, now=10) == (2, 1)
    assert s.texts == [] and len(queue_rows(tmp_path)) == 2 and "would send" in capsys.readouterr().out


# ---------- command line ----------
def test_main_status_reports_the_held_items_and_the_presence(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(qd, "LOGS", tmp_path)
    (tmp_path / qd.PRESENCE_NAME).write_text("asleep", encoding="utf-8")
    put(tmp_path, (1, "a"), (2, "b"))
    assert qd.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "held: 2 item(s)" in out and "presence asleep" in out


def test_main_delivers_when_awake_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(qd, "LOGS", tmp_path)
    put(tmp_path, (1, "morning digest"))
    assert qd.main([]) == 0
    assert "delivered 1 item(s) in 1 message(s)" in capsys.readouterr().out
    assert "morning digest" in (tmp_path / "alerts.txt").read_text(encoding="utf-8")
    assert qd.main([]) == 0 and "nothing to deliver" in capsys.readouterr().out


def test_main_dry_run_and_a_crash_both_exit_zero_and_keep_the_items(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(qd, "LOGS", tmp_path)
    put(tmp_path, (1, "a"))
    assert qd.main(["--dry-run"]) == 0
    assert "dry run, would deliver 1 item(s)" in capsys.readouterr().out and len(queue_rows(tmp_path)) == 1

    def explode(**kw):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(qd, "flush", explode)
    assert qd.main([]) == 0
    assert "could not deliver" in capsys.readouterr().out and len(queue_rows(tmp_path)) == 1
