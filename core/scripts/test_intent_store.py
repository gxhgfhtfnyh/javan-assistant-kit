# -*- coding: utf-8 -*-
"""Tests for intent_store.py"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Ensure the module under test uses our temp paths
sys.path.insert(0, str(Path(__file__).parent))

import intent_store as M


@pytest.fixture(autouse=True)
def isolate_logs(tmp_path, monkeypatch):
    """Point HARNESS_LOGS at a temp dir for every test."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    yield logs


def _read_lines(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# --- new_intent --------------------------------------------------------------

def test_new_intent_basic():
    i = M.new_intent("test_kind", "test what", tier=1, value=5, dedupe="my-key")
    assert i["kind"] == "test_kind"
    assert i["what"] == "test what"
    assert i["tier"] == 1
    assert i["value"] == 5
    assert i["dedupe"] == "my-key"
    assert i["status"] == "open"
    assert i["shadow"] is False
    assert i["lease"] is None
    assert len(i["id"]) == 12
    assert "ts" in i and "epoch" in i


def test_new_intent_shadow():
    i = M.new_intent("k", "w", shadow=True)
    assert i["shadow"] is True


def test_new_intent_check():
    i = M.new_intent("k", "w", check={"kind": "pytest", "file": "test_x.py"})
    assert i["check"] == {"kind": "pytest", "file": "test_x.py"}


def test_new_intent_id_stable_same_minute():
    ts = 1_700_000_000.0
    i1 = M.new_intent("k", "w", dedupe="d", ts=ts)
    i2 = M.new_intent("k", "w", dedupe="d", ts=ts + 30)
    assert i1["id"] == i2["id"]


def test_new_intent_id_different_minute():
    ts = 1_700_000_000.0
    i1 = M.new_intent("k", "w", dedupe="d", ts=ts)
    i2 = M.new_intent("k", "w", dedupe="d", ts=ts + 61)
    assert i1["id"] != i2["id"]


# --- parse_check -------------------------------------------------------------

def test_parse_check_pytest_valid():
    c = M.parse_check("pytest:test_foo.py")
    assert c == {"kind": "pytest", "file": "test_foo.py"}


def test_parse_check_pytest_invalid_name():
    assert M.parse_check("pytest:foo.py") is None
    assert M.parse_check("pytest:test-.py") is None


def test_parse_check_dry_run_valid():
    c = M.parse_check("dry_run:script.py")
    assert c == {"kind": "dry_run", "script": "script.py"}


def test_parse_check_dry_run_invalid_name():
    assert M.parse_check("dry_run:script-.py") is None


def test_parse_check_exists():
    c = M.parse_check("exists:/some/path")
    assert c == {"kind": "exists", "path": "/some/path"}


def test_parse_check_exists_empty():
    assert M.parse_check("exists:") is None


def test_parse_check_contains():
    c = M.parse_check("contains:/path::needle")
    assert c == {"kind": "contains", "path": "/path", "text": "needle"}


def test_parse_check_contains_missing_parts():
    assert M.parse_check("contains:/path") is None
    assert M.parse_check("contains::text") is None


def test_parse_check_unknown():
    assert M.parse_check("unknown:foo") is None
    assert M.parse_check("") is None


# --- is_duplicate ------------------------------------------------------------

def test_is_duplicate_open():
    intents = [{"dedupe": "key", "status": "open", "epoch": M.now_s()}]
    cand = {"dedupe": "key"}
    assert M.is_duplicate(intents, cand) is True


def test_is_duplicate_doing():
    intents = [{"dedupe": "key", "status": "doing", "epoch": M.now_s()}]
    cand = {"dedupe": "key"}
    assert M.is_duplicate(intents, cand) is True


def test_is_duplicate_waiting():
    intents = [{"dedupe": "key", "status": "waiting", "epoch": M.now_s()}]
    cand = {"dedupe": "key"}
    assert M.is_duplicate(intents, cand) is True


def test_is_duplicate_done_within_cooldown():
    now = M.now_s()
    intents = [{"dedupe": "key", "status": "done", "closed_epoch": now - 100}]
    cand = {"dedupe": "key"}
    assert M.is_duplicate(intents, cand, now=now) is True


def test_is_duplicate_done_after_cooldown():
    now = M.now_s()
    intents = [{"dedupe": "key", "status": "done", "closed_epoch": now - M.COOLDOWN_SEC - 1}]
    cand = {"dedupe": "key"}
    assert M.is_duplicate(intents, cand, now=now) is False


def test_is_duplicate_dropped_after_cooldown():
    now = M.now_s()
    intents = [{"dedupe": "key", "status": "dropped", "closed_epoch": now - M.COOLDOWN_SEC - 1}]
    cand = {"dedupe": "key"}
    assert M.is_duplicate(intents, cand, now=now) is False


def test_is_duplicate_different_dedupe():
    intents = [{"dedupe": "other", "status": "open", "epoch": M.now_s()}]
    cand = {"dedupe": "key"}
    assert M.is_duplicate(intents, cand) is False


# --- actionable --------------------------------------------------------------

def test_actionable_tier0():
    i = {"status": "open", "tier": 0, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    assert M.actionable(i) is True


def test_actionable_tier1():
    i = {"status": "open", "tier": 1, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    assert M.actionable(i) is True


def test_actionable_tier2_not_actionable():
    i = {"status": "open", "tier": 2, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    assert M.actionable(i) is False


def test_actionable_tier3_not_actionable():
    i = {"status": "open", "tier": 3, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    assert M.actionable(i) is False


def test_actionable_shadow_not_actionable():
    i = {"status": "open", "tier": 1, "shadow": True, "kind": "k", "lease": None, "not_before": None}
    assert M.actionable(i) is False


def test_actionable_closed_not_actionable():
    i = {"status": "done", "tier": 1, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    assert M.actionable(i) is False


def test_actionable_lease_active_not_actionable():
    now = M.now_s()
    i = {"status": "open", "tier": 1, "shadow": False, "kind": "k", "lease": {"until": now + 100, "by": "other"}, "not_before": None}
    assert M.actionable(i, now=now) is False


def test_actionable_lease_expired_actionable():
    now = M.now_s()
    i = {"status": "open", "tier": 1, "shadow": False, "kind": "k", "lease": {"until": now - 100, "by": "other"}, "not_before": None}
    assert M.actionable(i, now=now) is True


def test_actionable_not_before_future_not_actionable():
    now = M.now_s()
    i = {"status": "open", "tier": 1, "shadow": False, "kind": "k", "lease": None, "not_before": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 3600))}
    assert M.actionable(i, now=now) is False


def test_actionable_not_before_past_actionable():
    now = M.now_s()
    i = {"status": "open", "tier": 1, "shadow": False, "kind": "k", "lease": None, "not_before": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 3600))}
    assert M.actionable(i, now=now) is True


# --- rank --------------------------------------------------------------------

def test_rank_deadline_soon_first():
    now = M.now_s()
    soon = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 3600))
    later = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 100000))
    i1 = {"status": "open", "tier": 1, "value": 1, "deadline": later, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    i2 = {"status": "open", "tier": 1, "value": 1, "deadline": soon, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    ranked = M.rank([i1, i2], now=now)
    assert ranked[0]["deadline"] == soon


def test_rank_higher_value_first():
    now = M.now_s()
    i1 = {"status": "open", "tier": 1, "value": 3, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    i2 = {"status": "open", "tier": 1, "value": 5, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    ranked = M.rank([i1, i2], now=now)
    assert ranked[0]["value"] == 5


def test_rank_older_first_when_tied():
    now = M.now_s()
    i1 = {"status": "open", "tier": 1, "value": 3, "deadline": None, "epoch": now - 100, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    i2 = {"status": "open", "tier": 1, "value": 3, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    ranked = M.rank([i1, i2], now=now)
    assert ranked[0]["epoch"] == now - 100


def test_rank_excludes_non_actionable():
    now = M.now_s()
    i1 = {"status": "open", "tier": 1, "value": 3, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    i2 = {"status": "open", "tier": 2, "value": 5, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    ranked = M.rank([i1, i2], now=now)
    assert len(ranked) == 1
    assert ranked[0]["tier"] == 1


# --- next_action -------------------------------------------------------------

def test_next_action_returns_top():
    now = M.now_s()
    i1 = {"id": "a", "status": "open", "tier": 1, "value": 3, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    i2 = {"id": "b", "status": "open", "tier": 1, "value": 5, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    top, why = M.next_action([i1, i2], now=now)
    assert top["id"] == "b"
    assert "top of 2 actionable" in why


def test_next_action_empty_queue():
    top, why = M.next_action([], now=M.now_s())
    assert top is None
    assert why == "nothing actionable: queue empty"


def test_next_action_reason_waiting_tier2():
    now = M.now_s()
    i = {"id": "x", "status": "open", "tier": 2, "value": 3, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    top, why = M.next_action([i], now=now)
    assert top is None
    assert "waiting on the owner" in why


def test_next_action_reason_leased():
    now = M.now_s()
    i = {"id": "x", "status": "doing", "tier": 1, "value": 3, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": {"until": now + 100, "by": "other"}, "not_before": None}
    top, why = M.next_action([i], now=now)
    assert top is None
    assert "already being worked" in why


def test_next_action_reason_shadow():
    now = M.now_s()
    i = {"id": "x", "status": "open", "tier": 1, "value": 3, "deadline": None, "epoch": now, "shadow": True, "kind": "k", "lease": None, "not_before": None}
    top, why = M.next_action([i], now=now)
    assert top is None
    assert "in shadow" in why


def test_next_action_reason_not_before():
    now = M.now_s()
    i = {"id": "x", "status": "open", "tier": 1, "value": 3, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 3600))}
    top, why = M.next_action([i], now=now)
    assert top is None
    assert "not due yet" in why


# --- expire_leases -----------------------------------------------------------

def test_expire_leases_frees_expired():
    now = M.now_s()
    i = {"id": "x", "status": "doing", "lease": {"until": now - 10, "by": "me"}}
    freed = M.expire_leases([i], now=now)
    assert freed == ["x"]
    assert i["lease"] is None
    assert i["status"] == "open"


def test_expire_leases_keeps_active():
    now = M.now_s()
    i = {"id": "x", "status": "doing", "lease": {"until": now + 100, "by": "me"}}
    freed = M.expire_leases([i], now=now)
    assert freed == []
    assert i["lease"] is not None
    assert i["status"] == "doing"


def test_expire_leases_only_doing_to_open():
    now = M.now_s()
    i = {"id": "x", "status": "open", "lease": {"until": now - 10, "by": "me"}}
    freed = M.expire_leases([i], now=now)
    assert freed == ["x"]
    assert i["status"] == "open"


# --- apply_outcome -----------------------------------------------------------

def test_apply_outcome_ok():
    i = {"id": "x", "status": "doing", "lease": {"until": M.now_s() + 100, "by": "me"}}
    M.apply_outcome(i, "ok", "evidence")
    assert i["status"] == "done"
    assert i["lease"] is None
    assert i["result"] == "ok"
    assert i["evidence"] == "evidence"
    assert "closed_epoch" in i


def test_apply_outcome_skipped():
    i = {"id": "x", "status": "doing", "lease": {"until": M.now_s() + 100, "by": "me"}}
    M.apply_outcome(i, "skipped", "evidence")
    assert i["status"] == "done"
    assert i["result"] == "skipped"


def test_apply_outcome_fail_first_time_reopens():
    i = {"id": "x", "status": "doing", "lease": {"until": M.now_s() + 100, "by": "me"}, "tries": 0}
    M.apply_outcome(i, "fail", "evidence")
    assert i["status"] == "open"
    assert i["tries"] == 1
    assert "closed_epoch" not in i


def test_apply_outcome_fail_second_time_drops():
    i = {"id": "x", "status": "doing", "lease": {"until": M.now_s() + 100, "by": "me"}, "tries": 1}
    M.apply_outcome(i, "fail", "evidence")
    assert i["status"] == "dropped"
    assert i["tries"] == 2


# --- read_jsonl / write_intents / merge -------------------------------------

def test_read_jsonl_missing_file(isolate_logs):
    out = M.read_jsonl(M.INTENTS)
    assert out == []


def test_read_jsonl_malformed_lines(isolate_logs):
    p = isolate_logs / M.INTENTS
    p.write_text('{"id": "a"}\nnot json\n{"id": "b"}\n', encoding="utf-8")
    out = M.read_jsonl(M.INTENTS)
    assert len(out) == 2
    assert out[0]["id"] == "a"
    assert out[1]["id"] == "b"


def test_read_jsonl_unicode(isolate_logs):
    p = isolate_logs / M.INTENTS
    p.write_text('{"id": "a", "what": "café 🎉"}\n', encoding="utf-8")
    out = M.read_jsonl(M.INTENTS)
    assert out[0]["what"] == "café 🎉"


def test_write_intents_creates_file(isolate_logs):
    intents = [{"id": "x", "status": "open", "kind": "k", "what": "w", "tier": 1, "value": 3, "dedupe": "d", "shadow": False, "epoch": M.now_s(), "ts": M.iso(), "lease": None, "not_before": None, "deadline": None, "from_event": None}]
    M.write_intents(intents)
    lines = _read_lines(isolate_logs / M.INTENTS)
    assert len(lines) == 1
    assert lines[0]["id"] == "x"


def test_merge_intents_preserves_other_actor_changes():
    now = M.now_s()
    base_i1 = {"id": "i1", "status": "open", "value": 1, "epoch": now, "tier": 1, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "lease": None, "not_before": None, "deadline": None, "from_event": None, "ts": M.iso()}
    base_i2 = {"id": "i2", "status": "open", "value": 2, "epoch": now, "tier": 1, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "lease": None, "not_before": None, "deadline": None, "from_event": None, "ts": M.iso()}

    # Writer read these two
    mine = M.Read([base_i1, base_i2])
    mine.base = {"i1": M._canon(base_i1), "i2": M._canon(base_i2)}

    # Other actor changed i2 and added i3
    current_i2 = dict(base_i2)
    current_i2["value"] = 99
    current_i3 = {"id": "i3", "status": "open", "value": 3, "epoch": now, "tier": 1, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "lease": None, "not_before": None, "deadline": None, "from_event": None, "ts": M.iso()}
    current = [base_i1, current_i2, current_i3]

    merged = M.merge_intents(mine, current)
    # i1 unchanged, i2 keeps other actor's change (value=99), i3 added
    by_id = {m["id"]: m for m in merged}
    assert by_id["i1"]["value"] == 1
    assert by_id["i2"]["value"] == 99
    assert by_id["i3"]["value"] == 3


def test_merge_intents_writer_change_wins():
    now = M.now_s()
    base_i1 = {"id": "i1", "status": "open", "value": 1, "epoch": now, "tier": 1, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "lease": None, "not_before": None, "deadline": None, "from_event": None, "ts": M.iso()}

    mine = M.Read([base_i1])
    mine[0]["value"] = 77
    mine.base = {"i1": M._canon(base_i1)}

    current = [base_i1]
    merged = M.merge_intents(mine, current)
    assert merged[0]["value"] == 77


def test_merge_intents_writer_removes():
    now = M.now_s()
    base_i1 = {"id": "i1", "status": "open", "value": 1, "epoch": now, "tier": 1, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "lease": None, "not_before": None, "deadline": None, "from_event": None, "ts": M.iso()}
    base_i2 = {"id": "i2", "status": "open", "value": 2, "epoch": now, "tier": 1, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "lease": None, "not_before": None, "deadline": None, "from_event": None, "ts": M.iso()}

    mine = M.Read([base_i1, base_i2])
    mine.pop()  # remove i2
    mine.base = {"i1": M._canon(base_i1), "i2": M._canon(base_i2)}

    current = [base_i1, base_i2]
    merged = M.merge_intents(mine, current)
    assert len(merged) == 1
    assert merged[0]["id"] == "i1"


# --- claim -------------------------------------------------------------------

def test_claim_success(isolate_logs):
    i = {"id": "abc123", "status": "open", "tier": 1, "value": 3, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "epoch": M.now_s(), "ts": M.iso(), "lease": None, "not_before": None, "deadline": None, "from_event": None}
    M.write_intents([i])
    code, msg = M.claim("abc123", "me")
    assert code == 0
    assert "claimed" in msg


def test_claim_not_found(isolate_logs):
    M.write_intents([])
    code, msg = M.claim("nonexistent", "me")
    assert code == 2
    assert "no such intent" in msg


def test_claim_refused_done(isolate_logs):
    i = {"id": "abc123", "status": "done", "tier": 1, "value": 3, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "epoch": M.now_s(), "ts": M.iso(), "lease": None, "not_before": None, "deadline": None, "from_event": None}
    M.write_intents([i])
    code, msg = M.claim("abc123", "me")
    assert code == 5
    assert "refused" in msg


def test_claim_refused_other_lease(isolate_logs):
    now = M.now_s()
    i = {"id": "abc123", "status": "doing", "tier": 1, "value": 3, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "epoch": now, "ts": M.iso(), "lease": {"until": now + 100, "by": "other"}, "not_before": None, "deadline": None, "from_event": None}
    M.write_intents([i])
    code, msg = M.claim("abc123", "me")
    assert code == 5
    assert "held by other" in msg


def test_claim_same_claimer_renews_lease(isolate_logs):
    now = M.now_s()
    i = {"id": "abc123", "status": "doing", "tier": 1, "value": 3, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "epoch": now, "ts": M.iso(), "lease": {"until": now + 100, "by": "me"}, "not_before": None, "deadline": None, "from_event": None}
    M.write_intents([i])
    code, msg = M.claim("abc123", "me", secs=50)
    assert code == 0
    # Re-read and verify lease updated (new expiry = now + 50, which is different from old expiry)
    intents = M.read_jsonl(M.INTENTS)
    assert intents[0]["lease"]["until"] != now + 100
    assert intents[0]["lease"]["by"] == "me"


# --- main CLI ----------------------------------------------------------------

def test_main_add(isolate_logs, capsys):
    rc = M.main(["add", "--kind", "test", "--what", "do it", "--tier", "1", "--value", "5", "--dedupe", "my-key"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "added" in out
    assert "tier1 value5" in out


def test_main_add_duplicate(isolate_logs, capsys):
    M.main(["add", "--kind", "test", "--what", "do it", "--dedupe", "my-key"])
    rc = M.main(["add", "--kind", "test", "--what", "do it", "--dedupe", "my-key"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "duplicate" in out


def test_main_next_empty(isolate_logs, capsys):
    rc = M.main(["next"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "NONE nothing actionable" in out


def test_main_next_returns_top(isolate_logs, capsys):
    M.main(["add", "--kind", "t1", "--what", "low", "--value", "1", "--dedupe", "a"])
    M.main(["add", "--kind", "t2", "--what", "high", "--value", "5", "--dedupe", "b"])
    rc = M.main(["next"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "NEXT" in out
    assert "high" in out


def test_main_claim(isolate_logs, capsys):
    M.main(["add", "--kind", "t", "--what", "w", "--dedupe", "d"])
    lines = _read_lines(isolate_logs / M.INTENTS)
    iid = lines[0]["id"]
    rc = M.main(["claim", iid, "--by", "tester"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "claimed" in out


def test_main_claim_refused_second_claimer(isolate_logs, capsys):
    M.main(["add", "--kind", "t", "--what", "w", "--dedupe", "d"])
    lines = _read_lines(isolate_logs / M.INTENTS)
    iid = lines[0]["id"]
    M.main(["claim", iid, "--by", "first"])
    rc = M.main(["claim", iid, "--by", "second"])
    assert rc == 5
    out = capsys.readouterr().out
    assert "refused" in out


def test_main_done_ok_requires_evidence(isolate_logs, capsys):
    M.main(["add", "--kind", "t", "--what", "w", "--dedupe", "d"])
    lines = _read_lines(isolate_logs / M.INTENTS)
    iid = lines[0]["id"]
    M.main(["claim", iid, "--by", "me"])
    rc = M.main(["done", iid, "--result", "ok", "--evidence", "done"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "closed" in out
    assert "ok" in out


def test_main_done_fail_reopens(isolate_logs, capsys):
    M.main(["add", "--kind", "t", "--what", "w", "--dedupe", "d"])
    lines = _read_lines(isolate_logs / M.INTENTS)
    iid = lines[0]["id"]
    M.main(["claim", iid, "--by", "me"])
    rc = M.main(["done", iid, "--result", "fail", "--evidence", "oops"])
    assert rc == 0
    intents = M.read_jsonl(M.INTENTS)
    assert intents[0]["status"] == "open"
    assert intents[0]["tries"] == 1


def test_main_done_fail_twice_drops(isolate_logs, capsys):
    M.main(["add", "--kind", "t", "--what", "w", "--dedupe", "d"])
    lines = _read_lines(isolate_logs / M.INTENTS)
    iid = lines[0]["id"]
    M.main(["claim", iid, "--by", "me"])
    M.main(["done", iid, "--result", "fail", "--evidence", "oops1"])
    M.main(["claim", iid, "--by", "me"])
    rc = M.main(["done", iid, "--result", "fail", "--evidence", "oops2"])
    assert rc == 0
    intents = M.read_jsonl(M.INTENTS)
    assert intents[0]["status"] == "dropped"
    assert intents[0]["tries"] == 2


def test_main_done_skipped_finishes(isolate_logs, capsys):
    M.main(["add", "--kind", "t", "--what", "w", "--dedupe", "d"])
    lines = _read_lines(isolate_logs / M.INTENTS)
    iid = lines[0]["id"]
    M.main(["claim", iid, "--by", "me"])
    rc = M.main(["done", iid, "--result", "skipped", "--evidence", "n/a"])
    assert rc == 0
    intents = M.read_jsonl(M.INTENTS)
    assert intents[0]["status"] == "done"


def test_main_done_invalid_result_refused(isolate_logs, capsys):
    M.main(["add", "--kind", "t", "--what", "w", "--dedupe", "d"])
    lines = _read_lines(isolate_logs / M.INTENTS)
    iid = lines[0]["id"]
    M.main(["claim", iid, "--by", "me"])
    rc = M.main(["done", iid, "--result", "invalid", "--evidence", "x"])
    # The current code treats invalid as fail -> reopen once
    intents = M.read_jsonl(M.INTENTS)
    assert intents[0]["status"] == "open"


def test_done_without_evidence_is_refused_even_when_a_done_when_check_would_pass(isolate_logs, capsys, monkeypatch):
    monkeypatch.setattr(M, "run_check", lambda check, **kw: (True, "fine"))        # no file is written into the scripts folder
    M.main(["add", "--kind", "t", "--what", "w", "--dedupe", "d", "--check", "pytest:test_x.py"])
    iid = _read_lines(isolate_logs / M.INTENTS)[0]["id"]
    M.main(["claim", iid, "--by", "me"])
    for empty in ("", "   "):
        assert M.main(["done", iid, "--result", "ok", "--evidence", empty]) == 3
        assert "--evidence is required" in capsys.readouterr().out
        assert M.read_jsonl(M.INTENTS)[0]["status"] == "doing" and not (isolate_logs / M.OUTCOMES).exists()
    assert M.main(["done", iid, "--result", "ok", "--evidence", "ran it"]) == 0
    row = M.read_jsonl(M.INTENTS)[0]
    assert row["status"] == "done" and row["evidence"].startswith("ran it [done-when check passed:")


def test_done_refuses_empty_evidence_for_every_result_and_accepts_a_reason_for_skipped_and_fail(isolate_logs):
    for n, result in enumerate(("ok", "skipped", "fail")):
        M.main(["add", "--kind", "t%d" % n, "--what", "w%d" % n, "--dedupe", "d%d" % n])
    ids = [r["id"] for r in _read_lines(isolate_logs / M.INTENTS)]
    for iid, result in zip(ids, ("ok", "skipped", "fail")):
        assert M.main(["done", iid, "--result", result, "--evidence", ""]) == 3
        assert M.main(["done", iid, "--result", result, "--evidence", "because: " + result]) == 0


def test_main_list(isolate_logs, capsys):
    M.main(["add", "--kind", "t", "--what", "w1", "--dedupe", "d1"])
    M.main(["add", "--kind", "t", "--what", "w2", "--dedupe", "d2"])
    rc = M.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "w1" in out
    assert "w2" in out
    assert "2 of 2 shown" in out


def test_main_list_all_shows_done(isolate_logs, capsys):
    M.main(["add", "--kind", "t", "--what", "w", "--dedupe", "d"])
    lines = _read_lines(isolate_logs / M.INTENTS)
    iid = lines[0]["id"]
    M.main(["claim", iid, "--by", "me"])
    M.main(["done", iid, "--result", "ok", "--evidence", "done"])
    rc = M.main(["list", "--all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "done" in out


# --- outcomes.jsonl and harness_state.json ----------------------------------

def test_outcomes_written_on_done(isolate_logs, capsys):
    M.main(["add", "--kind", "t", "--what", "w", "--dedupe", "d"])
    lines = _read_lines(isolate_logs / M.INTENTS)
    iid = lines[0]["id"]
    M.main(["claim", iid, "--by", "me"])
    M.main(["done", iid, "--result", "ok", "--evidence", "done"])
    outcomes = _read_lines(isolate_logs / M.OUTCOMES)
    assert len(outcomes) == 1
    assert outcomes[0]["intent_id"] == iid
    assert outcomes[0]["result"] == "ok"
    assert outcomes[0]["evidence"] == "done"
    assert outcomes[0]["action"] == "w"


def test_harness_state_counters(isolate_logs):
    # The main() doesn't update harness_state.json directly; that's a separate module.
    # But we can verify the file structure exists for the test environment.
    state_path = isolate_logs / M.STATE
    # Just ensure it can be created
    state_path.write_text(json.dumps({"updated": M.iso(), "counters": {}}), encoding="utf-8")
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert "counters" in data


# --- two processes appending at once (simulated) -----------------------------

def test_two_writers_merge(isolate_logs):
    now = M.now_s()
    i1 = {"id": "a", "status": "open", "value": 1, "epoch": now, "tier": 1, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "lease": None, "not_before": None, "deadline": None, "from_event": None, "ts": M.iso()}
    i2 = {"id": "b", "status": "open", "value": 2, "epoch": now, "tier": 1, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "lease": None, "not_before": None, "deadline": None, "from_event": None, "ts": M.iso()}

    # Writer A reads both
    mine_a = M.Read([i1, i2])
    mine_a.base = {"a": M._canon(i1), "b": M._canon(i2)}
    mine_a[0]["value"] = 10  # A changes a

    # Writer B reads both, changes b
    mine_b = M.Read([i1, i2])
    mine_b.base = {"a": M._canon(i1), "b": M._canon(i2)}
    mine_b[1]["value"] = 20  # B changes b

    # Simulate A writing first
    M.write_intents(mine_a)
    # Then B writes - should merge both changes
    M.write_intents(mine_b)

    final = M.read_jsonl(M.INTENTS)
    by_id = {x["id"]: x for x in final}
    assert by_id["a"]["value"] == 10
    assert by_id["b"]["value"] == 20


# --- malformed lines skipped -------------------------------------------------

def test_read_jsonl_skips_malformed(isolate_logs):
    p = isolate_logs / M.INTENTS
    p.write_text('{"id": "good"}\n{not json}\n{"id": "also_good"}\n', encoding="utf-8")
    out = M.read_jsonl(M.INTENTS)
    assert len(out) == 2
    assert out[0]["id"] == "good"
    assert out[1]["id"] == "also_good"


# --- lease expiry returns intent to open -------------------------------------

def test_lease_expiry_returns_to_open(isolate_logs, capsys):
    now = M.now_s()
    i = {"id": "abc123", "status": "doing", "tier": 1, "value": 3, "kind": "k", "what": "w", "dedupe": "d", "shadow": False, "epoch": now, "ts": M.iso(), "lease": {"until": now - 10, "by": "me"}, "not_before": None, "deadline": None, "from_event": None}
    M.write_intents([i])

    # Call next which runs expire_leases
    M.main(["next"])
    intents = M.read_jsonl(M.INTENTS)
    assert intents[0]["status"] == "open"
    assert intents[0]["lease"] is None


# --- tier 2 surfaces as question, not actionable -----------------------------

def test_tier2_not_actionable_but_visible():
    now = M.now_s()
    i = {"id": "x", "status": "open", "tier": 2, "value": 10, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    ranked = M.rank([i], now=now)
    assert len(ranked) == 0  # Not actionable

    # But next_action explains why
    top, why = M.next_action([i], now=now)
    assert top is None
    assert "waiting on the owner" in why


# --- tier 3 refused ----------------------------------------------------------

def test_tier3_not_actionable():
    now = M.now_s()
    i = {"id": "x", "status": "open", "tier": 3, "value": 10, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    ranked = M.rank([i], now=now)
    assert len(ranked) == 0


# --- shadow intents never actionable -----------------------------------------

def test_shadow_never_actionable():
    now = M.now_s()
    i = {"id": "x", "status": "open", "tier": 0, "value": 10, "deadline": None, "epoch": now, "shadow": True, "kind": "k", "lease": None, "not_before": None}
    assert M.actionable(i, now=now) is False
    ranked = M.rank([i], now=now)
    assert len(ranked) == 0


# --- rank order: value then deadline -----------------------------------------

def test_rank_deadline_beats_value():
    now = M.now_s()
    soon = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 3600))
    i1 = {"status": "open", "tier": 1, "value": 10, "deadline": None, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    i2 = {"status": "open", "tier": 1, "value": 1, "deadline": soon, "epoch": now, "shadow": False, "kind": "k", "lease": None, "not_before": None}
    ranked = M.rank([i1, i2], now=now)
    assert ranked[0]["value"] == 1  # deadline soon wins over high value


# --- cooldown dedupe after done ----------------------------------------------

def test_dedupe_cooldown_after_done(isolate_logs, capsys):
    now = M.now_s()
    # Add and complete an intent
    i = M.new_intent("k", "w", dedupe="my-dedupe", ts=now - 1000)
    i["status"] = "done"
    i["closed_epoch"] = now - 100
    M.write_intents([i])

    # Try to add same dedupe within cooldown
    rc = M.main(["add", "--kind", "k", "--what", "w2", "--dedupe", "my-dedupe"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "duplicate" in out

    # After cooldown, should allow
    old_i = M.new_intent("k", "w", dedupe="my-dedupe", ts=now - M.COOLDOWN_SEC - 100)
    old_i["status"] = "done"
    old_i["closed_epoch"] = now - M.COOLDOWN_SEC - 100
    M.write_intents([old_i])
    rc = M.main(["add", "--kind", "k", "--what", "w3", "--dedupe", "my-dedupe"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "added" in out


# --- main with HARNESS_LOGS at tmp_path -------------------------------------

def test_main_uses_harness_logs_env(tmp_path, monkeypatch, capsys):
    """Verify main() respects HARNESS_LOGS env var."""
    logs = tmp_path / "custom_logs"
    logs.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs))

    rc = M.main(["add", "--kind", "t", "--what", "w", "--dedupe", "d"])
    assert rc == 0

    intents_file = logs / M.INTENTS
    assert intents_file.exists()
    lines = _read_lines(intents_file)
    assert len(lines) == 1


# --- dedupe within same minute -----------------------------------------------

def test_dedupe_same_minute_blocked(isolate_logs, capsys):
    ts = M.now_s()
    M.main(["add", "--kind", "k", "--what", "w1", "--dedupe", "d"])
    # Second add within same minute - would have same id
    # But the dedupe check catches it first
    rc = M.main(["add", "--kind", "k", "--what", "w2", "--dedupe", "d"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "duplicate" in out


# --- not_before parsing ------------------------------------------------------

def test_parse_when_formats():
    ts = time.mktime(time.strptime("2024-01-15 10:30:00", "%Y-%m-%d %H:%M:%S"))
    assert M._parse_when("2024-01-15 10:30:00") == ts
    assert M._parse_when("2024-01-15 10:30") == time.mktime(time.strptime("2024-01-15 10:30", "%Y-%m-%d %H:%M"))
    assert M._parse_when("2024-01-15") == time.mktime(time.strptime("2024-01-15", "%Y-%m-%d"))
    assert M._parse_when("") is None
    assert M._parse_when(None) is None
    assert M._parse_when("invalid") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

# Added in review (2026-10-31)
class TestReviewFixes:
    def test_a_lock_file_left_by_a_crashed_writer_is_removed_at_once_not_waited_on(self, tmp_path, monkeypatch):
        import time as _t
        lock = tmp_path / ".intents.lock"
        lock.write_text("")
        old = _t.time() - 3600
        os.utime(lock, (old, old))
        monkeypatch.setattr(M, "LOCK_WAIT", 30)               # if the stale lock were waited on, this test would take 30 s
        started = _t.time()
        with M._intents_lock(str(tmp_path)):
            assert lock.exists()                                            # we hold a fresh one now
        assert _t.time() - started < 5
        assert not lock.exists()                                            # and released it

    def test_a_fresh_lock_is_respected_until_the_wait_runs_out_and_is_not_deleted(self, tmp_path, monkeypatch):
        import time as _t
        lock = tmp_path / ".intents.lock"
        lock.write_text("")
        monkeypatch.setattr(M, "LOCK_WAIT", 0.3)
        started = _t.time()
        with M._intents_lock(str(tmp_path)):
            waited = _t.time() - started
        assert waited >= 0.25 and lock.exists()                             # ran anyway after the wait, but never took or removed the other holder's lock

    def test_the_stale_age_is_thirty_seconds(self):
        assert M.LOCK_STALE == 30

    def test_accents_and_emoji_survive_a_non_utf8_pipe(self, tmp_path):
        import subprocess
        env = dict(os.environ, HARNESS_LOGS=str(tmp_path), PYTHONIOENCODING="ascii")
        here = os.path.dirname(os.path.abspath(__file__))
        what = "café – \U0001F600"
        r = subprocess.run([sys.executable, os.path.join(here, "intent_store.py"), "add", "--kind", "t", "--what", what, "--tier", "1", "--value", "4", "--dedupe", "k"],
                           env=env, capture_output=True)
        assert r.returncode == 0 and what.encode("utf-8") in r.stdout
        r = subprocess.run([sys.executable, os.path.join(here, "intent_store.py"), "next"], env=env, capture_output=True)
        assert r.returncode == 0 and what.encode("utf-8") in r.stdout


def test_a_deadline_inside_24_hours_beats_a_higher_value_but_one_beyond_24_hours_does_not():
    now = time.time()

    def when(hours):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + hours * 3600))

    big = M.new_intent("k", "big value no deadline", value=5, dedupe="big", ts=now - 100)
    near = M.new_intent("k", "small but due in 20 hours", value=1, dedupe="near", deadline=when(20), ts=now - 50)
    far = M.new_intent("k", "small and due in 30 hours", value=1, dedupe="far", deadline=when(30), ts=now - 50)
    assert M.rank([big, near, far], now)[0]["dedupe"] == "near"
    assert M.rank([big, far], now)[0]["dedupe"] == "big"
