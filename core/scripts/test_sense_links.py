# -*- coding: utf-8 -*-
"""Tests for the link sensor's pure core (no network, no real log)."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sense_links as sl  # noqa: E402

OWNER = 555


def _recent_iso():
    """A timestamp always within the sensor's default 48h lookback, regardless of when the suite runs."""
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 3600))


def dm(text, mid=1, author=OWNER, source="dm", date=None):
    return {"id": mid, "text": text, "author_id": author, "source": source, "date": date or _recent_iso()}


def _configure(tmp_path, owner=OWNER):
    (tmp_path / "discord_config.json").write_text(json.dumps({"owner_id": owner}), encoding="utf-8")


def test_links_in_finds_each_link_once_and_strips_punctuation():
    text = "watch https://youtu.be/abc123 and https://youtu.be/abc123 and https://reddit.com/r/x/, thanks!"
    assert sl.links_in(text) == ["https://youtu.be/abc123", "https://reddit.com/r/x/"]
    assert sl.links_in("") == [] and sl.links_in(None) == []


def test_links_in_ignores_a_bare_word_and_keeps_query_strings():
    assert sl.links_in("youtu.be/abc no scheme") == []
    assert sl.links_in("https://youtu.be/a?si=xy") == ["https://youtu.be/a?si=xy"]


def test_classify_names_the_kind_and_keeps_account_hosts_separate():
    assert sl.classify("https://youtu.be/x") == ("video", 1)
    assert sl.classify("https://www.youtube.com/watch?v=x") == ("video", 1)
    assert sl.classify("https://github.com/a/b") == ("repo or file", 1)
    assert sl.classify("https://arxiv.org/abs/1") == ("paper", 1)
    assert sl.classify("https://chatgpt.com/share/x")[1] == 2
    assert sl.classify("https://www.instagram.com/reel/x")[1] == 2
    assert sl.classify("https://some.unknown.site/page") == ("page", 1)


def test_unprocessed_only_takes_the_owners_dms_and_skips_seen_links():
    rows = [dm("https://youtu.be/one", 1),
            dm("https://youtu.be/two", 2),
            dm("https://youtu.be/three", 3, author=999),              # a friend, not the owner
            dm("https://youtu.be/four", 4, source="guild")]           # a server channel
    got = [u for u, _m, _t in sl.unprocessed(rows, seen=["https://youtu.be/one"], owner_id=OWNER)]
    assert got == ["https://youtu.be/two"]


def test_unprocessed_needs_a_real_owner_id():
    rows = [dm("https://youtu.be/one", 1)]
    assert sl.unprocessed(rows, seen=[]) == []
    assert sl.unprocessed(rows, seen=[], owner_id=None) == []


def test_unprocessed_respects_the_cutoff_and_sorts_oldest_first():
    rows = [dm("https://a.test/1", 1, date="2026-10-30T02:00:00+00:00"),
            dm("https://b.test/2", 2, date="2026-10-29T02:00:00+00:00"),
            dm("https://c.test/3", 3, date="2026-10-20T02:00:00+00:00")]
    cutoff = sl._epoch("2026-10-28T00:00:00+00:00")
    got = [u for u, _m, _t in sl.unprocessed(rows, seen=[], owner_id=OWNER, cutoff=cutoff)]
    assert got == ["https://b.test/2", "https://a.test/1"]


def test_unprocessed_does_not_repeat_a_link_sent_twice_in_one_run():
    rows = [dm("https://youtu.be/same", 1), dm("again https://youtu.be/same", 2)]
    assert len(sl.unprocessed(rows, seen=[], owner_id=OWNER)) == 1


def test_unprocessed_survives_junk_rows():
    assert sl.unprocessed([None, "nope", {}, dm("https://ok.test/1", 1)], seen=[], owner_id=OWNER) != []


def test_not_set_up_without_an_owner_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sl, "LOGS", str(tmp_path))
    assert sl.main([]) == 0
    out = capsys.readouterr().out
    assert "not set up" in out and "owner_id" in out
    assert os.listdir(str(tmp_path)) == []


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    import intent_store
    _configure(tmp_path)
    monkeypatch.setattr(sl, "LOGS", str(tmp_path))
    monkeypatch.setattr(intent_store, "read_jsonl", lambda name, logs=None: [dm("https://youtu.be/x", 7)] if "incoming" in name else [])
    assert sl.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would raise tier1" in out and "nothing written" in out
    assert os.listdir(str(tmp_path)) == ["discord_config.json"]


def test_main_raises_one_intent_per_link_and_remembers_it(tmp_path, monkeypatch, capsys):
    import intent_store
    _configure(tmp_path)
    monkeypatch.setattr(sl, "LOGS", str(tmp_path))
    incoming = [dm("https://youtu.be/vid", 7), dm("https://chatgpt.com/share/s", 8)]
    monkeypatch.setattr(intent_store, "read_jsonl",
                        lambda name, logs=None: incoming if "incoming" in name else _real(name, tmp_path))
    assert sl.main([]) == 0
    assert "2 unread link(s), 2 new intent(s)" in capsys.readouterr().out
    intents = _real(intent_store.INTENTS, tmp_path)
    assert len(intents) == 2
    assert {i["tier"] for i in intents} == {1, 2}              # the chatgpt share needs the owner's account
    assert all(i["kind"] == "unread_link" for i in intents)
    assert len(_real(intent_store.EVENTS, tmp_path)) == 2
    # a second run sees the same links as already handled
    assert sl.main([]) == 0
    assert "0 new intent(s)" in capsys.readouterr().out


def _real(name, tmp_path):
    p = os.path.join(str(tmp_path), name)
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def test_kill_switch_stops_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sl, "LOGS", str(tmp_path))
    (tmp_path / sl.OFF).write_text("off", encoding="utf-8")
    assert sl.main([]) == 0 and "off" in capsys.readouterr().out
