# -*- coding: utf-8 -*-
"""Tests for voice_stats: features, stiffness, summaries, owner id, jsonl tail reading and the end-to-end report."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import voice_stats as vs  # noqa: E402


def iso(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


def test_features_counts_words_lowercase_lists_and_newlines():
    f = vs.features("ok so the thing works\n- first\n- second")
    assert f["words"] == 9 and f["list_lines"] == 2 and f["newlines"] == 2 and f["lower"] == 1.0


def test_features_capital_sentence_starts_and_formal_opener():
    f = vs.features("Certainly. Here is the summary of the work. It was done.")
    assert f["formal"] == 1 and f["cap_share"] == 1.0
    assert vs.features("yeah that works. then we ship")["cap_share"] == 0.0


def test_features_empty_text_and_none_are_safe():
    for t in ("", None):
        f = vs.features(t)
        assert f["words"] == 0 and f["lower"] == 1.0 and f["cap_share"] == 0.0


def test_features_numbered_and_bullet_lists_both_count():
    assert vs.features("1. one\n2) two\n* three")["list_lines"] == 3


def test_stiff_rules():
    assert vs.stiff(vs.features("- a\n- b"))
    assert vs.stiff(vs.features("Certainly, done"))
    assert vs.stiff(vs.features("The work is finished now. Every test passes. Nothing else remains to do here."))
    assert not vs.stiff(vs.features("yeah done, all tests pass"))
    assert not vs.stiff(vs.features("Short."))


def test_summarize_averages_and_handles_empty():
    assert vs.summarize([]) == {"n": 0}
    s = vs.summarize([vs.features("one two three"), vs.features("- list line here")])
    assert s["n"] == 2 and s["stiff_pct"] == 50 and s["avg_words"] == 3.5


def test_owner_id_reads_the_config_and_survives_every_bad_shape(tmp_path):
    cfg = tmp_path / "discord_config.json"
    assert vs.owner_id(tmp_path) is None
    cfg.write_text(json.dumps({"owner_id": 12345}), encoding="utf-8")
    assert vs.owner_id(tmp_path) == 12345
    cfg.write_text(json.dumps({"owner_id": "678"}), encoding="utf-8")
    assert vs.owner_id(tmp_path) == 678
    for bad in ("{ nope", json.dumps({}), json.dumps({"owner_id": "abc"}), json.dumps([1, 2])):
        cfg.write_text(bad, encoding="utf-8")
        assert vs.owner_id(tmp_path) is None


def test_tail_rows_skips_bad_lines_and_non_objects(tmp_path):
    p = tmp_path / "x.jsonl"
    write_jsonl(p, [{"a": 1}, "not json", "[1, 2]", {"b": 2}])
    assert vs.tail_rows(p) == [{"a": 1}, {"b": 2}]
    assert vs.tail_rows(tmp_path / "missing.jsonl") == []


def test_tail_rows_drops_the_cut_first_line_only_when_the_file_is_longer_than_the_window(tmp_path):
    p = tmp_path / "x.jsonl"
    write_jsonl(p, [{"n": i, "pad": "x" * 50} for i in range(40)])
    rows = vs.tail_rows(p, max_bytes=500)
    assert rows and rows[-1]["n"] == 39 and all(isinstance(r, dict) for r in rows)
    assert len(vs.tail_rows(p)) == 40


def test_main_without_an_owner_id_says_so_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path))
    assert vs.main() == 0
    assert "no owner_id" in capsys.readouterr().out


def test_main_compares_the_owner_with_the_assistant(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path))
    (tmp_path / "discord_config.json").write_text(json.dumps({"owner_id": 7}), encoding="utf-8")
    write_jsonl(tmp_path / "discord_incoming.jsonl", [
        {"author_id": 7, "source": "dm", "text": "hey are you there", "date": iso(60)},
        {"author_id": 99, "source": "dm", "text": "a stranger writing a very long message that must not be counted", "date": iso(60)},
        {"author_id": 7, "source": "guild", "text": "channel chatter that is not a dm", "date": iso(60)},
    ])
    write_jsonl(tmp_path / "discord_sent.jsonl", [{"text": "Certainly. Here is a summary of everything I did today for you.", "date": iso(30)}])
    assert vs.main() == 0
    out = capsys.readouterr().out
    assert "your messages" in out and "'n': 1" in out and "avg_words': 4.0" in out
    assert "assistant messages" in out and "stiff_pct': 100" in out
