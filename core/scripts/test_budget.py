# -*- coding: utf-8 -*-
"""Tests for the budget breaker: pure counters and trip, then check() on a tmp logs folder."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import budget as bg  # noqa: E402
import intent_store  # noqa: E402

NOW = 1_793_000_000.0


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t))


def test_tail_repeat_counts_only_the_end_of_the_list():
    assert bg.tail_repeat([]) == 0
    assert bg.tail_repeat(["a"]) == 1
    assert bg.tail_repeat(["a", "a", "b"]) == 1
    assert bg.tail_repeat(["b", "a", "a", "a"]) == 3


def test_trip_fires_at_the_limit_and_ignores_unmeasured_counters():
    t, which = bg.trip({"identical_calls": 3, "dms_per_day": 2, "usd_per_hour": None}, {"identical_calls": 3, "dms_per_day": 8, "usd_per_hour": 2.0})
    assert t and which == ["identical_calls"]
    assert bg.trip({"dms_per_day": 7}, {"dms_per_day": 8}) == (False, [])
    assert bg.trip({}, {"dms_per_day": 8}) == (False, [])


def test_counters_count_only_unprompted_dms_inside_the_window():
    sent = [
        {"date": iso(NOW - 100), "reply_to": None},
        {"date": iso(NOW - 200), "reply_to": 5},          # a reply is not unprompted
        {"date": iso(NOW - 90000), "reply_to": None},      # older than a day
        {"date": "garbage", "reply_to": None},
    ]
    c = bg.counters_from(NOW, sent, [{"ts": iso(NOW - 60), "result": "ok"}, {"ts": iso(NOW - 4000), "result": "ok"}, {"ts": iso(NOW - 30), "result": "skipped"}], [NOW - 10, NOW - 90000], ["x", "x"])
    assert c["dms_per_day"] == 1 and c["intents_per_hour"] == 1 and c["headless_per_day"] == 1 and c["identical_calls"] == 2
    assert c["usd_per_hour"] is None


def test_a_prompted_reply_split_into_chunks_does_not_inflate_dms_per_day():
    """A send layer can split one reply into several chunks, and only the first carries the channel's own
    reply_to (the rest must not show a second reply arrow) - but that would mean the continuation chunks get
    counted as fresh unprompted messages. The send script writes a chunk-independent "prompted" flag through
    every chunk of the same call; counters_from must skip those the same way it already skips a real reply_to."""
    sent = [
        {"date": iso(NOW - 100), "reply_to": 42},                  # chunk 1 of a 3-chunk reply
        {"date": iso(NOW - 99), "reply_to": None, "prompted": True},   # chunk 2
        {"date": iso(NOW - 98), "reply_to": None, "prompted": True},   # chunk 3
        {"date": iso(NOW - 50), "reply_to": None},                 # a genuinely unprompted ping
    ]
    c = bg.counters_from(NOW, sent, [], [], [])
    assert c["dms_per_day"] == 1


def test_the_same_text_sent_again_is_the_runaway_signal_not_volume():
    spam = [{"date": iso(NOW - 600 * i), "reply_to": None, "text": "Still on it: autonomy and speed with measured results. Done today: %d" % i} for i in range(4)]
    distinct = [{"date": iso(NOW - 300 * i), "reply_to": None, "text": "update number %d about something else entirely" % i} for i in range(20)]
    assert bg.counters_from(NOW, spam, [], [], [])["repeat_dms"] == 4   # same first 40 chars, different tail: this is what unwanted status-ping spam looks like
    spam2 = [{"date": iso(NOW - 600 * i), "reply_to": None, "text": "  Still on it: autonomy and SPEED with measured results"} for i in range(4)]
    c = bg.counters_from(NOW, spam2 + distinct, [], [], [])
    assert c["repeat_dms"] == 4 and c["dms_per_day"] == 24
    assert bg.trip(c, bg.DEFAULT_LIMITS) == (True, ["repeat_dms"])
    old = [{"date": iso(NOW - 7 * 3600), "reply_to": None, "text": "same same same same same same same same"}] * 5
    assert bg.counters_from(NOW, old, [], [], [])["repeat_dms"] == 0     # older than 6 hours does not count


def test_since_hides_history_from_before_the_breaker_was_armed():
    spam = [{"date": iso(NOW - 600 * i), "reply_to": None, "text": "same same same same same same same same same"} for i in range(6)]
    assert bg.counters_from(NOW, spam, [], [], [])["repeat_dms"] == 6
    armed = bg.counters_from(NOW, spam, [{"ts": iso(NOW - 100), "result": "ok"}], [NOW - 50], [], since=NOW - 1000)
    assert armed["repeat_dms"] == 2 and armed["dms_per_day"] == 2     # only rows after NOW-1000 count
    assert bg.counters_from(NOW, spam, [], [], [], since=NOW + 1)["dms_per_day"] == 0


def test_check_writes_the_switch_and_one_tier2_intent_then_stays_quiet(tmp_path, monkeypatch):
    logs = str(tmp_path)
    json.dump({"identical_calls": 3}, open(os.path.join(logs, "budget.json"), "w"))
    json.dump(["same", "same", "same"], open(os.path.join(logs, "budget_calls.json"), "w"))
    counters, which, new = bg.check(logs=logs, now=NOW)
    assert new == ["identical_calls"]
    assert which == ["identical_calls"] and counters["identical_calls"] == 3
    assert os.path.exists(os.path.join(logs, "loop_halt.txt"))
    intents = intent_store.read_jsonl(intent_store.INTENTS, logs)
    assert len(intents) == 1 and intents[0]["tier"] == 2 and intents[0]["kind"] == "budget_trip"
    assert bg.check(logs=logs, now=NOW)[2] == []          # second run: switch exists, nothing new, no second intent
    assert len(intent_store.read_jsonl(intent_store.INTENTS, logs)) == 1


def test_dry_run_changes_nothing(tmp_path):
    logs = str(tmp_path)
    json.dump(["s"] * 5, open(os.path.join(logs, "budget_calls.json"), "w"))
    _, which, _new = bg.check(logs=logs, dry_run=True, now=NOW)
    assert which == ["identical_calls"]
    assert not os.path.exists(os.path.join(logs, "loop_halt.txt"))
    assert not os.path.exists(os.path.join(logs, "intents.jsonl"))


def test_reset_removes_only_the_named_switch_and_starts_the_counters_afresh(tmp_path):
    logs = str(tmp_path)
    open(os.path.join(logs, "loop_halt.txt"), "w").write("x")
    assert bg.reset("identical_calls", logs, now=NOW) is True
    assert bg.reset("identical_calls", logs, now=NOW + 5) is False
    assert json.load(open(os.path.join(logs, "budget.json")))["since"] == NOW + 5


def test_reset_with_an_unknown_name_is_refused_not_turned_into_a_live_check(capsys):
    assert bg.main(["reset", "typo"]) == 2 and "reset needs one of" in capsys.readouterr().out


def test_note_call_keeps_only_the_last_twenty(tmp_path):
    for i in range(25):
        bg.note_call("c%d" % i, logs=str(tmp_path))
    calls = bg._last_calls(str(tmp_path))
    assert len(calls) == 20 and calls[-1] == "c24" and calls[0] == "c5"


def test_limits_file_overrides_defaults_but_ignores_unknown_keys(tmp_path):
    json.dump({"dms_per_day": 3, "bogus": 1}, open(os.path.join(str(tmp_path), "budget.json"), "w"))
    lim = bg.load_limits(str(tmp_path))
    assert lim["dms_per_day"] == 3 and "bogus" not in lim and lim["headless_per_day"] == 6
