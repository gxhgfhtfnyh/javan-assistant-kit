# -*- coding: utf-8 -*-
"""Tests for harness_health.py"""
import json
import os
import sys
import time
import pytest
from pathlib import Path

# Ensure we import from the current directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness_health
import intent_store


def make_intent(**kwargs):
    """Helper to create an intent dict with defaults."""
    defaults = {
        "id": "test123",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "epoch": time.time(),
        "kind": "test_kind",
        "what": "test what",
        "tier": 1,
        "value": 3,
        "dedupe": "test_kind",
        "status": "open",
        "shadow": False,
        "from_event": None,
        "lease": None,
        "not_before": None,
        "not_before_epoch": None,
    }
    defaults.update(kwargs)
    return defaults


def make_event(**kwargs):
    """Helper to create an event dict with defaults."""
    defaults = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "id": "evt123",
        "source": "sensor",
        "kind": "test_kind",
        "data": {"value": 3, "what": "test"},
    }
    defaults.update(kwargs)
    return defaults


class TestFellThrough:
    """Tests for the fell_through pure function."""

    def test_no_events_returns_zero_valued(self):
        """Empty events list yields zero valued and no lost."""
        valued, lost = harness_health.fell_through([], [], time.time())
        assert valued == 0
        assert lost == []

    def test_events_below_value_3_not_counted(self):
        """Events with value < 3 are ignored."""
        now = time.time()
        events = [make_event(data={"value": 2, "what": "low"})]
        valued, lost = harness_health.fell_through(events, [], now)
        assert valued == 0
        assert lost == []

    def test_events_value_3_counted_as_valued(self):
        """Events with value >= 3 are counted."""
        now = time.time()
        # Event from 10 minutes ago (past grace period)
        event_ts = now - 600
        events = [make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_ts)), data={"value": 3, "what": "high"})]
        valued, lost = harness_health.fell_through(events, [], now)
        assert valued == 1

    def test_event_younger_than_grace_skipped(self):
        """Events younger than grace period (default 300s) are skipped."""
        now = time.time()
        # Event from 100 seconds ago - younger than grace
        events = [make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 100)), data={"value": 5})]
        valued, lost = harness_health.fell_through(events, [], now)
        assert valued == 0
        assert lost == []

    def test_event_older_than_window_skipped(self):
        """Events older than window (default 86400s) are skipped."""
        now = time.time()
        # Event from 2 days ago
        events = [make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 2 * 86400)), data={"value": 5})]
        valued, lost = harness_health.fell_through(events, [], now)
        assert valued == 0
        assert lost == []

    def test_event_with_intent_in_cooldown_not_lost(self):
        """Event with a matching intent raised within cooldown is not lost."""
        now = time.time()
        # Event from 1 hour ago
        event_ts = now - 3600
        events = [make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_ts)), data={"value": 5})]
        # Intent raised 30 min before event (within cooldown of 6h)
        intent_ts = event_ts - 1800
        intents = [make_intent(epoch=intent_ts, kind="test_kind", status="open")]
        valued, lost = harness_health.fell_through(events, intents, now)
        assert valued == 1
        assert lost == []

    def test_event_with_intent_after_event_not_lost(self):
        """Event with intent raised after event but before now is not lost."""
        now = time.time()
        event_ts = now - 3600
        events = [make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_ts)), data={"value": 5})]
        # Intent raised 30 min after event
        intent_ts = event_ts + 1800
        intents = [make_intent(epoch=intent_ts, kind="test_kind", status="open")]
        valued, lost = harness_health.fell_through(events, intents, now)
        assert valued == 1
        assert lost == []

    def test_event_with_closed_intent_past_cooldown_is_lost(self):
        """Event with closed intent past cooldown IS lost."""
        now = time.time()
        event_ts = now - 3600
        events = [make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_ts)), data={"value": 5})]
        # Intent closed 7 hours and 100 seconds ago (past 6h cooldown, so cooldown ended 1h 100s ago)
        closed_ts = now - 7 * 3600 - 100
        intents = [make_intent(epoch=closed_ts - 100, closed_epoch=closed_ts, kind="test_kind", status="done")]
        valued, lost = harness_health.fell_through(events, intents, now)
        assert valued == 1
        assert len(lost) == 1

    def test_event_with_no_intent_is_lost(self):
        """Event with no intent of its kind is lost."""
        now = time.time()
        event_ts = now - 3600
        events = [make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_ts)), data={"value": 5})]
        valued, lost = harness_health.fell_through(events, [], now)
        assert valued == 1
        assert len(lost) == 1

    def test_from_event_matches_intent_kind(self):
        """Event's kind matches intent's from_event."""
        now = time.time()
        event_ts = now - 3600
        events = [make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_ts)), kind="event_kind", data={"value": 5})]
        intents = [make_intent(epoch=event_ts - 1800, kind="intent_kind", from_event="event_kind", status="open")]
        valued, lost = harness_health.fell_through(events, intents, now)
        assert valued == 1
        assert lost == []

    def test_multiple_events_some_lost_some_not(self):
        """Multiple events: some covered, some not."""
        now = time.time()
        event_ts = now - 3600
        events = [
            make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_ts)), kind="kind1", data={"value": 5}),
            make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_ts)), kind="kind2", data={"value": 5}),
        ]
        intents = [make_intent(epoch=event_ts - 1800, kind="kind1", status="open")]
        valued, lost = harness_health.fell_through(events, intents, now)
        assert valued == 2
        assert len(lost) == 1
        assert lost[0]["kind"] == "kind2"

    def test_unicode_in_event_what(self):
        """Unicode text in event what field works."""
        now = time.time()
        event_ts = now - 3600
        events = [make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_ts)), data={"value": 5, "what": "café 🎉"})]
        valued, lost = harness_health.fell_through(events, [], now)
        assert valued == 1
        assert lost[0]["data"]["what"] == "café 🎉"


class TestEvaluateA1:
    """Tests for A1 invariant evaluation."""

    def test_a1_green_no_high_value_work(self):
        """A1 green when no value>=4 intents waiting."""
        now = time.time()
        snap = {"intents": [], "hb_age": None, "autopilot_last_run": None}
        res = harness_health.evaluate(snap, now)
        assert res["A1"][0] == "green"

    def test_a1_green_session_fresh(self):
        """A1 green when session fresh (<15 min) even with high-value work."""
        now = time.time()
        intents = [make_intent(value=5, epoch=now - 40 * 60)]
        snap = {"intents": intents, "hb_age": 10 * 60, "autopilot_last_run": None}
        res = harness_health.evaluate(snap, now)
        assert res["A1"][0] == "green"
        assert "live session has it" in res["A1"][1]

    def test_a1_red_work_waiting_no_session(self):
        """A1 red when high-value work waiting >30 min and no session."""
        now = time.time()
        intents = [make_intent(value=5, epoch=now - 40 * 60)]
        snap = {"intents": intents, "hb_age": 20 * 60, "autopilot_last_run": None}  # session stale
        res = harness_health.evaluate(snap, now)
        assert res["A1"][0] == "red"
        assert "waiting over 30 min" in res["A1"][1]

    def test_a1_unmeasured_not_applicable(self):
        """A1 is never unmeasured - it's always green or red."""
        now = time.time()
        snap = {"intents": [], "hb_age": None}
        res = harness_health.evaluate(snap, now)
        assert res["A1"][0] in ("green", "red")

    def test_a1_not_before_future_excluded(self):
        """Intent with not_before in future is not stranded."""
        now = time.time()
        intents = [make_intent(value=5, epoch=now - 40 * 60, not_before_epoch=now + 3600)]
        snap = {"intents": intents, "hb_age": 20 * 60, "autopilot_last_run": None}
        res = harness_health.evaluate(snap, now)
        assert res["A1"][0] == "green"

    def test_a1_tier_2_not_counted(self):
        """Tier 2 intents are not auto-actionable, so not stranded."""
        now = time.time()
        intents = [make_intent(value=5, epoch=now - 40 * 60, tier=2)]
        snap = {"intents": intents, "hb_age": 20 * 60, "autopilot_last_run": None}
        res = harness_health.evaluate(snap, now)
        assert res["A1"][0] == "green"

    def test_a1_shadow_not_counted(self):
        """Shadow intents are not stranded."""
        now = time.time()
        intents = [make_intent(value=5, epoch=now - 40 * 60, shadow=True)]
        snap = {"intents": intents, "hb_age": 20 * 60, "autopilot_last_run": None}
        res = harness_health.evaluate(snap, now)
        assert res["A1"][0] == "green"


class TestEvaluateA2:
    """Tests for A2 invariant evaluation."""

    def test_a2_unmeasured_no_sensor_log(self):
        """A2 unmeasured when sensors_age is None."""
        now = time.time()
        snap = {"sensors_age": None}
        res = harness_health.evaluate(snap, now)
        assert res["A2"][0] == "unmeasured"
        assert "no sensor log" in res["A2"][1]

    def test_a2_green_sensor_recent(self):
        """A2 green when sensor logged <35 min ago."""
        now = time.time()
        snap = {"sensors_age": 30 * 60}
        res = harness_health.evaluate(snap, now)
        assert res["A2"][0] == "green"
        assert "30 min ago" in res["A2"][1]

    def test_a2_red_sensor_stale(self):
        """A2 red when sensor logged >35 min ago."""
        now = time.time()
        snap = {"sensors_age": 40 * 60}
        res = harness_health.evaluate(snap, now)
        assert res["A2"][0] == "red"
        assert "40 min ago" in res["A2"][1]

    def test_a2_boundary_at_35_min(self):
        """A2 green at exactly 35 min (strictly less than)."""
        now = time.time()
        snap = {"sensors_age": 35 * 60}
        res = harness_health.evaluate(snap, now)
        assert res["A2"][0] == "red"  # 35 min is not < 35 min


class TestEvaluateA3:
    """Tests for A3 invariant evaluation."""

    def test_a3_unmeasured_no_valued_events(self):
        """A3 unmeasured when no valued events in window."""
        now = time.time()
        snap = {"events": [], "intents": []}
        res = harness_health.evaluate(snap, now)
        assert res["A3"][0] == "unmeasured"
        assert "no sensor event" in res["A3"][1]

    def test_a3_green_all_covered(self):
        """A3 green when all valued events have intents."""
        now = time.time()
        event_ts = now - 3600
        events = [make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_ts)), data={"value": 5})]
        intents = [make_intent(epoch=event_ts - 1800, kind="test_kind", status="open")]
        snap = {"events": events, "intents": intents}
        res = harness_health.evaluate(snap, now)
        assert res["A3"][0] == "green"
        assert "all 1 valued event" in res["A3"][1]

    def test_a3_red_some_lost(self):
        """A3 red when some valued events have no intent."""
        now = time.time()
        event_ts = now - 3600
        events = [
            make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_ts)), kind="kind1", data={"value": 5}),
            make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event_ts)), kind="kind2", data={"value": 5}),
        ]
        intents = [make_intent(epoch=event_ts - 1800, kind="kind1", status="open")]
        snap = {"events": events, "intents": intents}
        res = harness_health.evaluate(snap, now)
        assert res["A3"][0] == "red"
        assert "1 of 2 valued event" in res["A3"][1]


class TestEvaluateA4:
    """Tests for A4 invariant evaluation."""

    def test_a4_red_gate_not_wired(self):
        """A4 red when send_gate_hook not in settings.json."""
        now = time.time()
        snap = {"gate_wired": False, "gate_off": False, "canary": {"ts": now, "ok": True, "detail": "ok"}}
        res = harness_health.evaluate(snap, now)
        assert res["A4"][0] == "red"
        assert "not in settings.json" in res["A4"][1]

    def test_a4_red_gate_off_switch(self):
        """A4 red when kill switch exists."""
        now = time.time()
        snap = {"gate_wired": True, "gate_off": True, "canary": {"ts": now, "ok": True, "detail": "ok"}}
        res = harness_health.evaluate(snap, now)
        assert res["A4"][0] == "red"
        assert "kill switch" in res["A4"][1]

    def test_a4_red_canary_ok_false(self):
        """A4 red when canary ok is false."""
        now = time.time()
        snap = {"gate_wired": True, "gate_off": False, "canary": {"ts": now, "ok": False, "detail": "failed"}}
        res = harness_health.evaluate(snap, now)
        assert res["A4"][0] == "red"
        assert "ok=false" in res["A4"][1]

    def test_a4_red_canary_old(self):
        """A4 red when canary older than 24 hours."""
        now = time.time()
        snap = {"gate_wired": True, "gate_off": False, "canary": {"ts": now - 25 * 3600, "ok": True, "detail": "ok"}}
        res = harness_health.evaluate(snap, now)
        assert res["A4"][0] == "red"
        assert "older than 24 hours" in res["A4"][1]

    def test_a4_unmeasured_canary_absent(self):
        """A4 unmeasured when canary file absent (canary is None)."""
        now = time.time()
        snap = {"gate_wired": True, "gate_off": False, "canary": None}
        res = harness_health.evaluate(snap, now)
        assert res["A4"][0] == "unmeasured"
        assert "file absent" in res["A4"][1]

    def test_a4_green_all_good(self):
        """A4 green when wired, no switch, canary ok and recent."""
        now = time.time()
        snap = {"gate_wired": True, "gate_off": False, "canary": {"ts": now, "ok": True, "detail": "20 of 20"}}
        res = harness_health.evaluate(snap, now)
        assert res["A4"][0] == "green"
        assert "wired, switch absent, canary ok" in res["A4"][1]


class TestEvaluateA5:
    """Tests for A5 invariant evaluation."""

    def test_a5_green_no_issues(self):
        """A5 green when no closed without evidence and no stale leases."""
        now = time.time()
        intents = [
            make_intent(status="done", closed_epoch=now - 1000, evidence="done"),
            make_intent(status="doing", lease={"until": now + 3600}),
        ]
        snap = {"intents": intents}
        res = harness_health.evaluate(snap, now)
        assert res["A5"][0] == "green"
        assert "every closed intent has evidence" in res["A5"][1]

    def test_a5_red_closed_without_evidence(self):
        """A5 red when closed intent lacks evidence."""
        now = time.time()
        intents = [make_intent(status="done", closed_epoch=now - 1000, evidence="")]
        snap = {"intents": intents}
        res = harness_health.evaluate(snap, now)
        assert res["A5"][0] == "red"
        assert "closed without evidence" in res["A5"][1]

    def test_a5_red_stale_lease(self):
        """A5 red when lease expired >10 min ago."""
        now = time.time()
        intents = [make_intent(status="doing", lease={"until": now - 700})]
        snap = {"intents": intents}
        res = harness_health.evaluate(snap, now)
        assert res["A5"][0] == "red"
        assert "lease(s) left past expiry" in res["A5"][1]

    def test_a5_canary_kind_excluded(self):
        """Canary kind intents are excluded from evidence check."""
        now = time.time()
        intents = [make_intent(kind="canary", status="done", closed_epoch=now - 1000, evidence="")]
        snap = {"intents": intents}
        res = harness_health.evaluate(snap, now)
        assert res["A5"][0] == "green"


class TestEvaluateA6:
    """Tests for A6 invariant evaluation."""

    def test_a6_unmeasured_no_learn_log(self):
        """A6 unmeasured when learner_age is None."""
        now = time.time()
        snap = {"learner_age": None}
        res = harness_health.evaluate(snap, now)
        assert res["A6"][0] == "unmeasured"
        assert "not run yet" in res["A6"][1]

    def test_a6_green_learn_recent(self):
        """A6 green when learn step ran <3 hours ago."""
        now = time.time()
        snap = {"learner_age": 2 * 3600}
        res = harness_health.evaluate(snap, now)
        assert res["A6"][0] == "green"
        assert "120 min ago" in res["A6"][1]

    def test_a6_red_learn_stale(self):
        """A6 red when learn step ran >3 hours ago."""
        now = time.time()
        snap = {"learner_age": 4 * 3600}
        res = harness_health.evaluate(snap, now)
        assert res["A6"][0] == "red"
        assert "240 min ago" in res["A6"][1]

    def test_a6_boundary_at_3_hours(self):
        """A6 red at exactly 3 hours (strictly less than)."""
        now = time.time()
        snap = {"learner_age": 3 * 3600}
        res = harness_health.evaluate(snap, now)
        assert res["A6"][0] == "red"


class TestEvaluateA7:
    """Tests for A7 invariant evaluation."""

    def test_a7_red_breaker_switch(self):
        """A7 red when any breaker switch exists."""
        now = time.time()
        snap = {"switches": ["loop_halt.txt"], "job_runner_age": 100}
        res = harness_health.evaluate(snap, now)
        assert res["A7"][0] == "red"
        assert "breaker switch set" in res["A7"][1]

    def test_a7_unmeasured_job_runner_never_existed(self):
        """A7 unmeasured when job_runner heartbeat never existed and no switches."""
        now = time.time()
        snap = {"switches": [], "job_runner_age": None}
        res = harness_health.evaluate(snap, now)
        assert res["A7"][0] == "unmeasured"
        assert "never existed" in res["A7"][1]

    def test_a7_red_job_runner_stale(self):
        """A7 red when job_runner heartbeat >20 min old."""
        now = time.time()
        snap = {"switches": [], "job_runner_age": 25 * 60}
        res = harness_health.evaluate(snap, now)
        assert res["A7"][0] == "red"
        assert "older than 20 minutes" in res["A7"][1]

    def test_a7_green_no_switches_job_runner_fresh(self):
        """A7 green when no switches and job_runner fresh."""
        now = time.time()
        snap = {"switches": [], "job_runner_age": 10 * 60}
        res = harness_health.evaluate(snap, now)
        assert res["A7"][0] == "green"
        assert "no breaker switch set" in res["A7"][1]


class TestEvaluateA8:
    """Tests for A8 invariant evaluation."""

    def test_a8_unmeasured_no_reflection_intent(self):
        """A8 unmeasured when no reflection intent raised."""
        now = time.time()
        snap = {"intents": []}
        res = harness_health.evaluate(snap, now)
        assert res["A8"][0] == "unmeasured"
        assert "no reflection intent raised" in res["A8"][1]

    def test_a8_red_stuck_open_over_8h(self):
        """A8 red when reflection intent left open >8 hours."""
        now = time.time()
        intents = [make_intent(kind="reflection", status="open", epoch=now - 9 * 3600)]
        snap = {"intents": intents}
        res = harness_health.evaluate(snap, now)
        assert res["A8"][0] == "red"
        assert "left open for over 8 hours" in res["A8"][1]

    def test_a8_green_recent_done(self):
        """A8 green when reflection closed recently (<26h)."""
        now = time.time()
        intents = [make_intent(kind="reflection", status="done", closed_epoch=now - 12 * 3600)]
        snap = {"intents": intents}
        res = harness_health.evaluate(snap, now)
        assert res["A8"][0] == "green"
        assert "being raised and closed" in res["A8"][1]

    def test_a8_green_fresh_open(self):
        """A8 green when reflection open but fresh (<8h)."""
        now = time.time()
        intents = [make_intent(kind="reflection", status="open", epoch=now - 2 * 3600)]
        snap = {"intents": intents}
        res = harness_health.evaluate(snap, now)
        assert res["A8"][0] == "green"

    def test_a8_red_no_recent_done_no_fresh_open(self):
        """A8 red when no reflection closed in 26h and none open."""
        now = time.time()
        intents = [make_intent(kind="reflection", status="done", closed_epoch=now - 27 * 3600)]
        snap = {"intents": intents}
        res = harness_health.evaluate(snap, now)
        assert res["A8"][0] == "red"
        assert "no reflection closed in the last 26 hours" in res["A8"][1]


class TestHealthNumber:
    """Tests for health_number pure function."""

    def test_all_green(self):
        """All green returns correct count."""
        res = {"A1": ("green", ""), "A2": ("green", ""), "A3": ("green", "")}
        green, measured = harness_health.health_number(res)
        assert green == 3
        assert measured == 3

    def test_some_red(self):
        """Some red counted in measured but not green."""
        res = {"A1": ("green", ""), "A2": ("red", ""), "A3": ("green", "")}
        green, measured = harness_health.health_number(res)
        assert green == 2
        assert measured == 3

    def test_unmeasured_excluded(self):
        """Unmeasured excluded from both green and measured."""
        res = {"A1": ("green", ""), "A2": ("unmeasured", ""), "A3": ("red", "")}
        green, measured = harness_health.health_number(res)
        assert green == 1
        assert measured == 2

    def test_all_unmeasured(self):
        """All unmeasured gives 0 green, 0 measured."""
        res = {"A1": ("unmeasured", ""), "A2": ("unmeasured", "")}
        green, measured = harness_health.health_number(res)
        assert green == 0
        assert measured == 0


class TestShouldAlarm:
    """Tests for should_alarm pure function."""

    def test_alarm_when_not_asleep(self):
        """Alarm fires when not asleep."""
        now = time.time()
        assert harness_health.should_alarm("A1", None, now, asleep=False) is True

    def test_alarm_suppressed_when_asleep_non_critical(self):
        """Alarm suppressed for non-critical when asleep."""
        now = time.time()
        assert harness_health.should_alarm("A1", None, now, asleep=True) is False

    def test_alarm_fires_when_asleep_critical(self):
        """Alarm fires for A4/A7 even when asleep."""
        now = time.time()
        assert harness_health.should_alarm("A4", None, now, asleep=True) is True
        assert harness_health.should_alarm("A7", None, now, asleep=True) is True

    def test_alarm_cooldown_respected(self):
        """Alarm suppressed within 6h cooldown."""
        now = time.time()
        last = now - 3 * 3600  # 3 hours ago
        assert harness_health.should_alarm("A1", last, now, asleep=False) is False

    def test_alarm_after_cooldown(self):
        """Alarm fires after 6h cooldown."""
        now = time.time()
        last = now - 7 * 3600  # 7 hours ago
        assert harness_health.should_alarm("A1", last, now, asleep=False) is True

    def test_alarm_no_last_alarm(self):
        """Alarm fires when no previous alarm."""
        now = time.time()
        assert harness_health.should_alarm("A1", None, now, asleep=False) is True


class TestGather:
    """Tests for gather function (integration with filesystem)."""

    def test_gather_reads_sensor_last(self, tmp_path, monkeypatch):
        """Gather reads sensor_last.txt."""
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "sensor_last.txt").write_text(str(time.time() - 1000))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        snap = harness_health.gather(str(logs))
        assert snap["sensors_age"] is not None
        assert 900 < snap["sensors_age"] < 1100

    def test_gather_sensor_last_absent_unmeasured(self, tmp_path, monkeypatch):
        """Gather returns None for sensors_age when sensor_last.txt absent."""
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        snap = harness_health.gather(str(logs))
        assert snap["sensors_age"] is None

    def test_gather_reads_learn_last(self, tmp_path, monkeypatch):
        """Gather reads learn_last.txt."""
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "learn_last.txt").write_text(str(time.time() - 2000))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        snap = harness_health.gather(str(logs))
        assert snap["learner_age"] is not None
        assert 1900 < snap["learner_age"] < 2100

    def test_gather_reads_canary(self, tmp_path, monkeypatch):
        """Gather reads send_gate_canary.json."""
        logs = tmp_path / "logs"
        logs.mkdir()
        canary = {"ts": time.time(), "ok": True, "detail": "all good"}
        (logs / "send_gate_canary.json").write_text(json.dumps(canary))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        snap = harness_health.gather(str(logs))
        assert snap["canary"] is not None
        assert snap["canary"]["ok"] is True

    def test_gather_canary_absent_none(self, tmp_path, monkeypatch):
        """Gather returns None for canary when file absent."""
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        snap = harness_health.gather(str(logs))
        assert snap["canary"] is None

    def test_gather_detects_gate_wired(self, tmp_path, monkeypatch):
        """Gather detects send_gate.py in settings.json PreToolUse hook."""
        logs = tmp_path / "logs"
        logs.mkdir()
        root = tmp_path
        (root / ".claude").mkdir()
        settings = {
            "hooks": {
                "PreToolUse": [
                    {"command": "python send_gate.py --check"}
                ]
            }
        }
        (root / ".claude" / "settings.json").write_text(json.dumps(settings))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        # Need to override SETTINGS path since it's computed from HERE
        harness_health.SETTINGS = str(root / ".claude" / "settings.json")
        snap = harness_health.gather(str(logs))
        assert snap["gate_wired"] is True

    def test_gather_gate_not_wired(self, tmp_path, monkeypatch):
        """Gather detects gate not wired."""
        logs = tmp_path / "logs"
        logs.mkdir()
        root = tmp_path
        (root / ".claude").mkdir()
        settings = {"hooks": {"PreToolUse": [{"command": "python other.py"}]}}
        (root / ".claude" / "settings.json").write_text(json.dumps(settings))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        harness_health.SETTINGS = str(root / ".claude" / "settings.json")
        snap = harness_health.gather(str(logs))
        assert snap["gate_wired"] is False

    def test_gather_gate_off_switch(self, tmp_path, monkeypatch):
        """Gather detects send_gate_off.txt."""
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "send_gate_off.txt").write_text("")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        snap = harness_health.gather(str(logs))
        assert snap["gate_off"] is True

    def test_gather_breaker_switches(self, tmp_path, monkeypatch):
        """Gather detects breaker switch files."""
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "loop_halt.txt").write_text("")
        (logs / "intents_auto_off.txt").write_text("")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        snap = harness_health.gather(str(logs))
        assert "loop_halt.txt" in snap["switches"]
        assert "intents_auto_off.txt" in snap["switches"]

    def test_gather_reads_intents_and_events(self, tmp_path, monkeypatch):
        """Gather reads intents and events from intent_store."""
        logs = tmp_path / "logs"
        logs.mkdir()
        intents_file = logs / "intents.jsonl"
        events_file = logs / "events.jsonl"
        now = time.time()
        intents_file.write_text(json.dumps(make_intent(epoch=now, id="i1")) + "\n")
        events_file.write_text(json.dumps(make_event(ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), id="e1")) + "\n")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        snap = harness_health.gather(str(logs))
        assert len(snap["intents"]) == 1
        assert len(snap["events"]) == 1


class TestAlarm:
    """Tests for alarm behavior."""

    def test_alarm_writes_intent_and_alerts(self, tmp_path, monkeypatch):
        """Alarm writes a tier-1 value-5 intent and appends to alerts.txt."""
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        harness_health._alarm(str(logs), "test alarm detail")
        # Check intent written
        intents = intent_store.read_jsonl(intent_store.INTENTS, str(logs))
        assert len(intents) == 1
        assert intents[0]["kind"] == "harness_red"
        assert intents[0]["tier"] == 1
        assert intents[0]["value"] == 5
        # Check alerts.txt
        alerts = (logs / "alerts.txt").read_text()
        assert "test alarm detail" in alerts

    def test_alarm_not_duplicate_same_day(self, tmp_path, monkeypatch):
        """Same dedupe key prevents duplicate intent same day."""
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        harness_health._alarm(str(logs), "test alarm detail")
        harness_health._alarm(str(logs), "test alarm detail again")
        intents = intent_store.read_jsonl(intent_store.INTENTS, str(logs))
        # Should only have one because dedupe includes date
        assert len(intents) == 1


class TestMain:
    """Tests for main CLI."""

    def test_main_dry_run(self, tmp_path, monkeypatch, capsys):
        """Main with --dry-run prints table and returns 0."""
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        # Need to patch SETTINGS
        root = tmp_path
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.json").write_text("{}")
        harness_health.SETTINGS = str(root / ".claude" / "settings.json")
        # Also patch LOGS
        harness_health.LOGS = str(logs)
        rc = harness_health.main(["--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "A1" in out
        assert "A8" in out
        assert "harness_health:" in out

    def test_main_check_mode_green(self, tmp_path, monkeypatch):
        """Main with --check exits 0 when all green."""
        logs = tmp_path / "logs"
        logs.mkdir()
        now = time.time()
        # Create green state: fresh sensor, canary ok, learn recent, no switches, job_runner fresh
        (logs / "sensor_last.txt").write_text(str(now - 100))
        (logs / "learn_last.txt").write_text(str(now - 100))
        (logs / "send_gate_canary.json").write_text(json.dumps({"ts": now, "ok": True, "detail": "ok"}))
        (logs / "heartbeats").mkdir()
        (logs / "heartbeats" / "job_runner.beat").write_text(str(now - 100))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        root = tmp_path
        (root / ".claude").mkdir()
        (root / "send_gate.py").write_text("# the gate\n")
        settings = {"hooks": {"PreToolUse": [{"command": "python send_gate.py --check"}]}}
        (root / ".claude" / "settings.json").write_text(json.dumps(settings))
        harness_health.SETTINGS = str(root / ".claude" / "settings.json")
        harness_health.LOGS = str(logs)
        rc = harness_health.main(["--check"])
        assert rc == 0

    def test_main_check_mode_red(self, tmp_path, monkeypatch):
        """Main with --check exits 1 when any red."""
        logs = tmp_path / "logs"
        logs.mkdir()
        # No sensor_last.txt -> A2 unmeasured, but we need a red
        # Create a stale learn_last to make A6 red
        (logs / "learn_last.txt").write_text(str(time.time() - 4 * 3600))
        (logs / "heartbeats").mkdir()
        (logs / "heartbeats" / "job_runner.beat").write_text(str(time.time() - 100))
        (logs / "send_gate_canary.json").write_text(json.dumps({"ts": time.time(), "ok": True, "detail": "ok"}))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        root = tmp_path
        (root / ".claude").mkdir()
        settings = {"hooks": {"PreToolUse": [{"command": "python send_gate.py --check"}]}}
        (root / ".claude" / "settings.json").write_text(json.dumps(settings))
        harness_health.SETTINGS = str(root / ".claude" / "settings.json")
        harness_health.LOGS = str(logs)
        rc = harness_health.main(["--check"])
        assert rc == 1


class TestNightRule:
    """Tests for night rule (presence_mode.txt contains asleep)."""

    def test_night_rule_suppresses_non_critical(self, tmp_path, monkeypatch):
        """When asleep, only A4 and A7 alarm."""
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "presence_mode.txt").write_text("asleep")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        asleep = harness_health._asleep(str(logs))
        assert asleep is True
        now = time.time()
        # A1 should be suppressed
        assert harness_health.should_alarm("A1", None, now, asleep) is False
        # A4 should alarm
        assert harness_health.should_alarm("A4", None, now, asleep) is True
        # A7 should alarm
        assert harness_health.should_alarm("A7", None, now, asleep) is True
        # A6 should be suppressed
        assert harness_health.should_alarm("A6", None, now, asleep) is False

    def test_not_asleep_allows_all(self, tmp_path, monkeypatch):
        """When not asleep, all alarms fire."""
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "presence_mode.txt").write_text("awake")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        asleep = harness_health._asleep(str(logs))
        assert asleep is False
        now = time.time()
        for k in ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"):
            assert harness_health.should_alarm(k, None, now, asleep) is True

    def test_presence_mode_absent_defaults_awake(self, tmp_path, monkeypatch):
        """Absent presence_mode.txt defaults to not asleep."""
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        asleep = harness_health._asleep(str(logs))
        assert asleep is False


class TestMalformedInputs:
    """Tests for handling malformed/missing files."""

    def test_malformed_json_in_canary(self, tmp_path, monkeypatch):
        """Malformed canary JSON treated as absent."""
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "send_gate_canary.json").write_text("not json")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        snap = harness_health.gather(str(logs))
        assert snap["canary"] is None

    def test_malformed_epoch_in_sensor_last(self, tmp_path, monkeypatch):
        """Non-numeric sensor_last.txt treated as absent."""
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "sensor_last.txt").write_text("not a number")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        snap = harness_health.gather(str(logs))
        assert snap["sensors_age"] is None

    def test_missing_logs_directory(self, tmp_path, monkeypatch):
        """Missing logs directory handled gracefully."""
        logs = tmp_path / "nonexistent"
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        snap = harness_health.gather(str(logs))
        assert snap["sensors_age"] is None
        assert snap["learner_age"] is None
        assert snap["canary"] is None
        assert snap["switches"] == []

    def test_unicode_in_alerts(self, tmp_path, monkeypatch):
        """Unicode in alarm text works."""
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        harness_health._alarm(str(logs), "café 🎉 alarm")
        alerts = (logs / "alerts.txt").read_text(encoding="utf-8")
        assert "café 🎉" in alerts


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

# Added in review (2026-10-31): the worker's tests used a flat hook entry; Claude Code nests the command under "hooks" (this is what the kit's settings.json.tpl writes).
class TestReviewFixes:
    NESTED = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": 'python "C:/x/scripts/send_gate.py"'}]}]}}

    def _logs(self, tmp_path, monkeypatch, settings):
        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        sp = tmp_path / "settings.json"
        sp.write_text(json.dumps(settings), encoding="utf-8")
        monkeypatch.setattr(harness_health, "SETTINGS", str(sp))
        return str(logs)

    def test_the_real_nested_hook_shape_counts_as_wired(self, tmp_path, monkeypatch):
        assert harness_health.gather(self._logs(tmp_path, monkeypatch, self.NESTED))["gate_wired"] is True

    def test_a_nested_hook_for_another_script_or_event_does_not_count(self, tmp_path, monkeypatch):
        other = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python other.py"}]}],
                           "Stop": [{"hooks": [{"type": "command", "command": "python send_gate.py"}]}]}}
        assert harness_health.gather(self._logs(tmp_path, monkeypatch, other))["gate_wired"] is False

    def test_garbled_settings_shapes_never_raise(self, tmp_path, monkeypatch):
        for bad in ({"hooks": []}, {"hooks": {"PreToolUse": "x"}}, {"hooks": {"PreToolUse": [1, None, {"hooks": "x"}, {"hooks": [3]}]}}, [], "text"):
            assert harness_health.gather(self._logs(tmp_path, monkeypatch, bad))["gate_wired"] is False

    def test_the_kits_own_rendered_settings_are_seen_as_wired(self, tmp_path, monkeypatch):
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".claude", "settings.json.tpl")
        if not os.path.exists(tpl):
            pytest.skip("run inside the kit: the hook wiring template is not next to this copy")
        text = open(tpl, encoding="utf-8").read().replace("{{root_path}}", "C:/assistant")
        assert harness_health.gather(self._logs(tmp_path, monkeypatch, json.loads(text)))["gate_wired"] is True

    def test_a4_is_red_for_a_set_kill_switch_even_before_the_first_canary(self):
        res = harness_health.evaluate({"gate_wired": True, "gate_off": True, "canary": None}, 1000.0)
        assert res["A4"][0] == "red" and "kill switch" in res["A4"][1]

    def test_a4_is_red_for_a_missing_wire_even_before_the_first_canary(self):
        res = harness_health.evaluate({"gate_wired": False, "gate_off": False, "canary": None}, 1000.0)
        assert res["A4"][0] == "red" and "not in settings.json" in res["A4"][1]

    def test_a4_lists_every_problem_at_once(self):
        res = harness_health.evaluate({"gate_wired": False, "gate_off": True, "canary": {"ts": 1.0, "ok": False, "detail": "boom"}}, 1000.0)
        assert res["A4"][0] == "red" and all(w in res["A4"][1] for w in ("not in settings.json", "kill switch", "boom"))


class TestA9CoverageHolds:
    """A live hook or scheduled script with literally no test file is the cheapest, plainest
    version of "this could be hiding something" - ties hygiene_audit.untested_live() into the
    same alarm pipeline as every other invariant instead of leaving it as a silent report."""

    def test_green_when_every_live_script_has_tests(self):
        assert harness_health.evaluate({"untested_live": []}, 1000.0)["A9"] == ("green", "every live script has a test file")

    def test_red_when_a_live_script_has_no_tests(self):
        res = harness_health.evaluate({"untested_live": ["some_hook.py"]}, 1000.0)
        assert res["A9"][0] == "red" and "some_hook.py" in res["A9"][1]

    def test_unmeasured_when_the_list_could_not_be_built(self):
        assert harness_health.evaluate({"untested_live": None}, 1000.0)["A9"][0] == "unmeasured"

    def test_message_lists_at_most_five_names(self):
        many = ["s%d.py" % i for i in range(12)]
        assert harness_health.evaluate({"untested_live": many}, 1000.0)["A9"][1].count(".py") == 5
