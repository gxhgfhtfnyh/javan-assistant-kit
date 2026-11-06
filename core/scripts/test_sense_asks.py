# -*- coding: utf-8 -*-
"""Tests for sense_asks: the pure rules (what is an ask, what answers it), reading logs and config, one run of the sensor, dedupe, limits and the command line."""

import os
import time
from pathlib import Path

import pytest


from sense_asks import (
    _clean,
    is_ask,
    substantive,
    answered,
    open_asks,
    to_epoch,
    normalize,
    keep,
    collect,
    NOT_ASK,
    ACK_ONLY,
)


class TestClean:
    def test_strips_control_chars_and_cuts(self):
        assert _clean("hello\x00world\x13test", 20) == "hello world test"
        assert len(_clean("a" * 200, 140)) == 140


class TestIsAsk:
    def test_short_and_ack_only_not_ask(self):
        assert is_ask("hi") is False
        assert is_ask("ok") is False

    def test_question_and_directive_is_ask(self):
        assert is_ask("can you help me with this?") is True
        assert is_ask("please run the tests for me") is True
        assert is_ask("make sure the build passes") is True

    def test_not_ask_filter_and_minimum(self):
        assert is_ask("please note this is important") is False
        assert NOT_ASK.search("what a nice day") is not None
        assert is_ask("i need a break") is False
        assert is_ask("do it") is False
        assert is_ask("can you do it") is True


class TestSubstantive:
    def test_short_and_narration_not_substantive(self):
        assert substantive("ok") is False
        assert substantive("i'm working on it now") is False

    def test_real_reply_is_substantive(self):
        assert substantive("here is the answer you asked for now") is True


class TestAnswered:
    def test_reply_to_match_and_mismatch(self):
        msg = {"id": "msg-123", "epoch": 1000.0}
        good = [{"reply_to": "msg-123", "text": "here is the detailed answer you wanted right now", "epoch": 2000.0}]
        bad = [{"reply_to": "msg-456", "text": "here is the detailed answer you wanted right now", "epoch": 2000.0}]
        assert answered(msg, good, "reply_to") is True
        assert answered(msg, bad, "reply_to") is False

    def test_next_reply_and_ack_only(self):
        msg = {"id": "msg-123", "epoch": 1000.0}
        later = [{"reply_to": None, "text": "here is the detailed answer you wanted right now", "epoch": 2000.0}]
        ack = [{"reply_to": "msg-123", "text": "ok", "epoch": 2000.0}]
        assert answered(msg, later, "next_reply") is True
        assert answered(msg, ack, "reply_to") is False


class TestOpenAsks:
    def test_finds_open_asks_after_grace_period(self):
        now = 10000.0
        incoming = [
            {"id": "1", "text": "can you help me with this task?", "epoch": now - 20 * 60},
            {"id": "2", "text": "what is the status?", "epoch": now - 5 * 60},
            {"id": "3", "text": "please run the tests", "epoch": now - 15 * 60},
        ]
        result = open_asks(incoming, [], now, 0.0, "reply_to")
        assert len(result) == 2
        assert {r[0] for r in result} == {"1", "3"}

    def test_excludes_answered_and_respects_since(self):
        now = 20000.0
        since = 9500.0
        incoming = [
            {"id": "1", "text": "can you help me?", "epoch": 9000.0},
            {"id": "2", "text": "what is the status?", "epoch": 10000.0},
        ]
        replies = [{"reply_to": "2", "text": "yes I can help you with that task right now", "epoch": 15000.0}]
        result = open_asks(incoming, replies, now, since, "reply_to")
        assert len(result) == 0

    def test_excludes_old_and_no_epoch(self):
        now = 10000.0
        incoming = [
            {"id": "1", "text": "can you help me?", "epoch": now - 25 * 3600},
            {"id": "2", "text": "what is the status?", "epoch": now - 15 * 60},
            {"id": "3", "text": "please run tests", "epoch": None},
        ]
        result = open_asks(incoming, [], now, 0.0, "reply_to")
        assert len(result) == 1
        assert result[0][0] == "2"


class TestToEpoch:
    def test_unix_and_iso(self):
        assert to_epoch(1000000000) == 1000000000.0
        assert to_epoch(1000000000000) == 1000000000.0
        assert to_epoch("2024-01-15T10:30:00Z") == 1705314600.0

    def test_invalid_returns_none(self):
        assert to_epoch("not-a-date") is None
        assert to_epoch(None) is None


class TestNormalizeAndKeep:
    def test_normalize_generates_id_and_truncates(self):
        fields = {"id": "id", "text": "text", "time": "date", "reply_to": "reply_to"}
        r1 = normalize({"text": "hello", "date": 1000000000}, fields)
        assert r1["id"].startswith("h") and len(r1["id"]) == 11
        r2 = normalize({"id": "a" * 100, "text": "hello", "date": 1000000000}, fields)
        assert len(r2["id"]) == 80

    def test_keep_filters(self):
        assert keep({"author_id": "123"}, {}) is True
        assert keep({"author_id": "123"}, {"author_id": "123"}) is True
        assert keep({"author_id": "456"}, {"author_id": "123"}) is False


class TestCollect:
    def test_collect_with_missing_logs_returns_notes(self, tmp_path, monkeypatch):
        import sense_asks
        monkeypatch.setattr(sense_asks, "ROOT", tmp_path)
        monkeypatch.setattr(sense_asks, "LOGS", tmp_path / "logs")
        monkeypatch.setattr(sense_asks, "logs_dir", lambda: tmp_path / "logs")

        cfg = {
            "incoming_log": "logs/user_messages.jsonl",
            "outgoing_log": "logs/assistant_messages.jsonl",
            "fields": {"id": "id", "text": "text", "time": "date", "reply_to": "reply_to"},
            "only": {},
            "match": "reply_to",
        }
        found, notes = collect(cfg, time.time(), 0.0)
        assert found == []
        assert "incoming log not found yet" in notes
        assert "outgoing log not found yet" in notes


# ---------- reading the logs and the config ----------
import json  # noqa: E402

import intent_store as ist  # noqa: E402
import sense_asks as sa  # noqa: E402

T0 = 1_000_000.0                                            # the moment the sensor is first armed in these tests


def jl(path, rows):
    Path(path).write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def setup_logs(tmp_path, monkeypatch, incoming, outgoing, **extra):
    """A logs folder with a config pointing at two JSON Lines files; the module reads its folders at call time through these names."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(sa, "LOGS", logs)
    monkeypatch.setattr(sa, "ROOT", tmp_path)
    jl(tmp_path / "in.jsonl", incoming)
    jl(tmp_path / "out.jsonl", outgoing)
    cfg = {"incoming_log": str(tmp_path / "in.jsonl"), "outgoing_log": str(tmp_path / "out.jsonl")}
    cfg.update(extra)
    (logs / sa.CONFIG).write_text(json.dumps(cfg), encoding="utf-8")
    return logs


def ask(i, text="can you check the backup folder and tell me what is in it?", t=T0 + 60):
    return {"id": "m%d" % i, "text": text, "date": t}


def intents(logs):
    return ist.read_jsonl(ist.INTENTS, str(logs))


class TestReadRows:
    def test_missing_file_is_none_and_bad_lines_are_skipped(self, tmp_path):
        assert sa.read_rows(tmp_path / "nope.jsonl") is None
        p = tmp_path / "x.jsonl"
        p.write_text("{ nope\n[1]\n\n" + json.dumps({"a": 1}) + "\n", encoding="utf-8")
        assert sa.read_rows(p) == [{"a": 1}]

    def test_keeps_only_the_last_rows_and_drops_a_cut_first_line(self, tmp_path):
        p = tmp_path / "x.jsonl"
        jl(p, [{"n": i, "pad": "x" * 40} for i in range(30)])
        assert [r["n"] for r in sa.read_rows(p, max_rows=3)] == [27, 28, 29]
        rows = sa.read_rows(p, max_bytes=300)
        assert rows[-1]["n"] == 29 and all("pad" in r for r in rows)


class TestLoadConfig:
    def test_missing_config_says_how_to_create_it(self, tmp_path):
        cfg, problem = sa.load_config(tmp_path)
        assert cfg is None and "create logs/message_log_config.json" in problem

    def test_bad_shapes_are_reported_not_raised(self, tmp_path):
        f = tmp_path / sa.CONFIG
        for text, word in (("{ nope", "not valid JSON"), ("[1]", "JSON object"), (json.dumps({"incoming_log": "a"}), "outgoing_log"), (json.dumps({"incoming_log": " ", "outgoing_log": "b"}), "incoming_log")):
            f.write_text(text, encoding="utf-8")
            cfg, problem = sa.load_config(tmp_path)
            assert cfg is None and word in problem
        f.write_text(" " * (sa.MAX_CONFIG_BYTES + 1), encoding="utf-8")
        assert sa.load_config(tmp_path)[0] is None

    def test_defaults_overrides_filters_and_match_mode(self, tmp_path):
        f = tmp_path / sa.CONFIG
        f.write_text(json.dumps({"incoming_log": " a.jsonl ", "outgoing_log": "b.jsonl"}), encoding="utf-8")
        cfg, problem = sa.load_config(tmp_path)
        assert problem == "" and cfg["incoming_log"] == "a.jsonl" and cfg["fields"] == sa.DEFAULT_FIELDS and cfg["only"] == {} and cfg["match"] == "reply_to"
        f.write_text(json.dumps({"incoming_log": "a", "outgoing_log": "b", "fields": {"text": "body", "bogus": "x", "id": 5}, "only": {"author": "me", "n": 3, "flag": True, "bad": [1]}, "match": "next_reply"}), encoding="utf-8")
        cfg, _ = sa.load_config(tmp_path)
        assert cfg["fields"]["text"] == "body" and cfg["fields"]["id"] == "id" and "bogus" not in cfg["fields"]
        assert cfg["only"] == {"author": "me", "n": 3} and cfg["match"] == "next_reply"
        f.write_text(json.dumps({"incoming_log": "a", "outgoing_log": "b", "match": "whatever"}), encoding="utf-8")
        assert sa.load_config(tmp_path)[0]["match"] == "reply_to"


# ---------- one run of the sensor ----------
class TestRun:
    def test_it_is_off_with_the_switch_file_but_a_dry_run_still_reports(self, tmp_path, monkeypatch, capsys):
        logs = setup_logs(tmp_path, monkeypatch, [ask(1)], [])
        (logs / sa.OFF).write_text("off", encoding="utf-8")
        assert sa._run(False, T0) == 0 and capsys.readouterr().out.strip() == "sense_asks: off"
        assert not (logs / sa.STATE).exists()
        sa._run(True, T0)
        assert "nothing written" in capsys.readouterr().out

    def test_without_a_config_it_says_how_to_set_up_and_writes_nothing(self, tmp_path, monkeypatch, capsys):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setattr(sa, "LOGS", logs)
        assert sa._run(False, T0) == 0
        assert "set up sense_asks" in capsys.readouterr().out and not (logs / sa.STATE).exists()

    def test_the_first_run_only_arms_and_replays_no_history(self, tmp_path, monkeypatch, capsys):
        logs = setup_logs(tmp_path, monkeypatch, [ask(1, t=T0 - 5000)], [])
        assert sa._run(False, T0) == 0
        assert "armed" in capsys.readouterr().out
        assert json.loads((logs / sa.STATE).read_text(encoding="utf-8"))["since"] == T0
        assert intents(logs) == []

    def test_an_open_ask_becomes_one_work_item_and_is_never_raised_twice(self, tmp_path, monkeypatch, capsys):
        logs = setup_logs(tmp_path, monkeypatch, [ask(1)], [])
        sa._save_state({"since": T0, "raised": []}, logs)
        sa._run(False, T0 + 60 + sa.GRACE + 60)
        (item,) = intents(logs)
        assert item["kind"] == "ask_open" and item["tier"] == 1 and item["value"] == 5 and item["dedupe"] == "ask-m1"
        assert "msg m1" in item["what"] and "check the backup folder" in item["what"]
        assert json.loads((logs / sa.STATE).read_text(encoding="utf-8"))["raised"] == ["m1"]
        assert "1 new intent(s)" in capsys.readouterr().out
        sa._run(False, T0 + 5000)
        assert len(intents(logs)) == 1 and "0 new intent(s)" in capsys.readouterr().out

    def test_an_ask_raised_before_is_not_raised_again_even_if_its_item_is_gone(self, tmp_path, monkeypatch):
        logs = setup_logs(tmp_path, monkeypatch, [ask(1)], [])
        sa._save_state({"since": T0, "raised": ["m1"]}, logs)
        sa._run(False, T0 + 5000)
        assert intents(logs) == []

    def test_a_real_reply_closes_the_ask_but_a_bare_acknowledgement_does_not(self, tmp_path, monkeypatch):
        answer = {"id": "r1", "reply_to": "m1", "date": T0 + 120, "text": "i checked the backup folder and it holds two files from last week, both fine"}
        acknowledgement = {"id": "r2", "reply_to": "m2", "date": T0 + 120, "text": "ok"}
        logs = setup_logs(tmp_path, monkeypatch, [ask(1), ask(2)], [answer, acknowledgement])
        sa._save_state({"since": T0, "raised": []}, logs)
        sa._run(False, T0 + 5000)
        assert [i["dedupe"] for i in intents(logs)] == ["ask-m2"]

    def test_an_ask_younger_than_the_grace_period_waits(self, tmp_path, monkeypatch):
        logs = setup_logs(tmp_path, monkeypatch, [ask(1)], [])
        sa._save_state({"since": T0, "raised": []}, logs)
        sa._run(False, T0 + 60 + sa.GRACE - 30)
        assert intents(logs) == []

    def test_at_most_ten_items_are_raised_per_run(self, tmp_path, monkeypatch):
        logs = setup_logs(tmp_path, monkeypatch, [ask(i, t=T0 + 60 + i) for i in range(1, 15)], [])
        sa._save_state({"since": T0, "raised": []}, logs)
        sa._run(False, T0 + 5000)
        assert len(intents(logs)) == sa.MAX_PER_RUN
        sa._run(False, T0 + 6000)
        assert len(intents(logs)) == 14

    def test_the_text_in_the_item_is_cut_and_cleaned(self, tmp_path, monkeypatch):
        text = "can you do this" + chr(0) + chr(7) + " " + "very long " * 40 + "?"
        logs = setup_logs(tmp_path, monkeypatch, [ask(1, text=text)], [])
        sa._save_state({"since": T0, "raised": []}, logs)
        sa._run(False, T0 + 5000)
        what = intents(logs)[0]["what"]
        assert chr(0) not in what and chr(7) not in what and "very long" in what
        shown = what.split('"')[1]
        assert len(shown) <= sa.TEXT_MAX + 3

    def test_a_dry_run_lists_what_it_would_raise_and_writes_nothing(self, tmp_path, monkeypatch, capsys):
        logs = setup_logs(tmp_path, monkeypatch, [ask(1)], [])
        sa._save_state({"since": T0, "raised": []}, logs)
        before = (logs / sa.STATE).read_text(encoding="utf-8")
        sa._run(True, T0 + 5000)
        out = capsys.readouterr().out
        assert "would raise" in out and "1 finding(s), nothing written" in out
        assert intents(logs) == [] and (logs / sa.STATE).read_text(encoding="utf-8") == before

    def test_state_keeps_only_the_newest_raised_ids(self, tmp_path):
        sa._save_state({"since": 1.0, "raised": [str(i) for i in range(sa.RAISED_KEEP + 25)]}, tmp_path)
        saved = json.loads((tmp_path / sa.STATE).read_text(encoding="utf-8"))
        assert len(saved["raised"]) == sa.RAISED_KEEP and saved["raised"][-1] == str(sa.RAISED_KEEP + 24)
        assert sa._load_state(tmp_path)["since"] == 1.0
        (tmp_path / sa.STATE).write_text("[1]", encoding="utf-8")
        assert sa._load_state(tmp_path) == {}


class TestMain:
    def test_main_never_breaks_the_caller(self, monkeypatch, capsys):
        def boom(dry, now):
            raise RuntimeError("disk gone")
        monkeypatch.setattr(sa, "_run", boom)
        assert sa.main([]) == 0 and "stopped (RuntimeError)" in capsys.readouterr().out

    def test_main_dry_run_without_a_config_returns_zero(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sa, "LOGS", tmp_path)
        assert sa.main(["--dry-run"]) == 0 and "set up sense_asks" in capsys.readouterr().out
