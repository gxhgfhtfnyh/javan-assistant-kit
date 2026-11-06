# -*- coding: utf-8 -*-
"""Tests for the learn step: labelling, rates, proposals, apply/release, and the whole command on a temporary folder."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import learn_outcomes as lo  # noqa: E402
import intent_store  # noqa: E402

T0 = 1_793_000_000.0


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "vault" / "05 - Resources").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def demoted(kind, logs):
    """intent_store caches the overrides file by path and modification time; clear the cache so a coarse file clock cannot make a test read stale data."""
    intent_store._OVERRIDES.update(mtime=None, path=None, data={})
    return intent_store.kind_demoted(kind, logs)


def mk_outcome(iid, t, result="ok"):
    return {"ts": t, "intent_id": iid, "action": "x", "evidence": "e", "result": result, "reaction": None}


def mk_intent(iid, kind, **kw):
    d = {"id": iid, "kind": kind, "what": "write the report file", "tier": 1, "value": 3, "status": "done", "shadow": False}
    d.update(kw)
    return d


def jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def read_jsonl(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------- labels ----------
def test_label_priority_correction_over_nudge_over_approval_over_none():
    assert lo.label([]) == "none" and lo.label(None) == "none"
    assert lo.label(["lol ok"]) == "none" and lo.label(["ok"]) == "approval"
    assert lo.label(["thanks that works"]) == "approval"
    assert lo.label(["keep going", "thanks"]) == "nudge"
    assert lo.label(["thanks", "that is wrong, you forgot the second file"]) == "correction"
    assert lo.label(["password again"]) == "none"                     # "again" alone is not a correction
    assert lo.label(["i did not ask for that"]) == "correction"


def test_a_correction_must_share_a_content_word_with_the_action_but_a_nudge_always_counts():
    mine = lo.tokens("Fix the stray backticks in the capabilities rows example")
    assert lo.label(["why are you looking at the calendar stuff, that is wrong"], mine) == "none"           # about something else
    assert lo.label(["the backticks fix is wrong, you forgot the second one"], mine) == "correction"
    assert lo.label(["you keep stopping"], mine) == "nudge"
    assert lo.label(["that is wrong"], None) == "correction"                                                # no tokens given: any correction counts


def test_tokens_drop_short_and_generic_words():
    assert lo.tokens("The harness intent script about calendar scanning") == {"calendar", "scanning"}
    assert lo.tokens(None) == set()


def test_labels_wait_for_a_complete_window_and_use_only_messages_inside_it():
    intents = [mk_intent("a", "k1"), mk_intent("b", "k1"), mk_intent("c", "k1")]
    outcomes = [mk_outcome("a", T0), mk_outcome("b", T0), mk_outcome("c", T0 + 1000, result="fail")]
    prompts = [{"ts": T0 + 60, "text": "that is wrong, the report is missing"}, {"ts": T0 + 4000, "text": "wrong again"}]
    got = lo.attach_labels(outcomes, intents, prompts, now=T0 + 2000)
    assert [(r["intent_id"], r["label"]) for r in got] == [("a", "correction"), ("b", "correction")]
    assert lo.attach_labels(outcomes, intents, prompts, now=T0 + 100) == []                       # window not complete yet
    assert lo.attach_labels(outcomes, intents, prompts, now=T0 + 2000, already={"a", "b"}) == []  # never labelled twice


def test_a_later_message_outside_the_window_does_not_label_an_outcome():
    got = lo.attach_labels([mk_outcome("a", T0)], [mk_intent("a", "k")], [{"ts": T0 + 3000, "text": "wrong report"}], now=T0 + 5000)
    assert got[0]["label"] == "none"


def test_an_outcome_from_before_the_prompt_log_began_is_not_judged():
    got = lo.attach_labels([mk_outcome("a", T0), mk_outcome("b", T0 + 5000)], [mk_intent("a", "k"), mk_intent("b", "k")], [], now=T0 + 9000, feed_start=T0 + 1000)
    assert [g["intent_id"] for g in got] == ["b"]


def test_internal_kinds_are_not_learned_from():
    intents = [mk_intent("a", "canary"), mk_intent("b", "budget_trip"), mk_intent("c", "real"), mk_intent("d", "intent_kind_review")]
    outs = [mk_outcome(i, T0) for i in ("a", "b", "c", "d")]
    assert [g["kind"] for g in lo.attach_labels(outs, intents, [], now=T0 + 3000)] == ["real"]


def test_an_outcome_without_a_known_intent_or_with_a_bad_timestamp_is_skipped():
    outs = [mk_outcome("ghost", T0), {"ts": "not a time", "intent_id": "a", "result": "ok"}, {"intent_id": "a", "result": "ok"}, "junk", mk_outcome("a", T0)]
    got = lo.attach_labels(outs, [mk_intent("a", "k")], [], now=T0 + 3000)
    assert [g["intent_id"] for g in got] == ["a"]


def test_timestamps_may_be_iso_strings_in_local_time():
    t = time.mktime(time.strptime("2026-10-30 10:00:00", "%Y-%m-%d %H:%M:%S"))
    out = {"ts": "2026-10-30 10:00:00", "intent_id": "a", "result": "ok"}
    got = lo.attach_labels([out], [mk_intent("a", "k")], [{"ts": t + 60, "text": "you keep stopping"}], now=t + 4000)
    assert got[0]["label"] == "nudge"


# ---------- rates and proposals ----------
def test_rates_count_corrections_and_nudges_as_not_accepted():
    labeled = [{"kind": "k", "label": l} for l in ("none", "approval", "none", "correction", "nudge")] + [{"kind": "j", "label": "none"}, {"label": "none"}, "junk"]
    r = lo.rates(labeled)
    assert r["k"] == (5, 3, 0.6) and r["j"] == (1, 1, 1.0) and len(r) == 2


def test_proposals_need_five_closed_and_do_not_repeat_themselves():
    table = {"bad": (6, 2, 2 / 6), "good": (6, 6, 1.0), "few": (4, 0, 0.0), "mid": (10, 7, 0.7)}
    props = lo.proposals(table, {})
    assert {(p["kind"], p["action"]) for p in props} == {("bad", "demote"), ("good", "promote")}
    again = lo.proposals(table, {"bad": {"shadow": True}, "good": {"bonus": 1}})
    assert again == []


# ---------- apply and release ----------
def test_apply_demotes_through_the_overrides_file_and_intent_store_obeys(env):
    logs = str(env / "logs")
    ledger = env / "ledger.md"
    ledger.write_text("# ledger\n", encoding="utf-8")
    intent_store.write_intents([mk_intent("1", "bad", status="open")], logs)
    ov = lo.apply([{"kind": "bad", "action": "demote", "rate": 0.2, "closed": 5}], logs=logs, ledger=ledger)
    assert ov["bad"]["shadow"] is True
    assert "intent-demotion" in ledger.read_text(encoding="utf-8")
    assert demoted("bad", logs) is True
    got = intent_store.read_jsonl(intent_store.INTENTS, logs)[0]
    assert intent_store.actionable(got) is False


def test_apply_raises_the_value_of_open_intents_of_a_promoted_kind_up_to_four_and_skips_tier_two(env):
    logs = str(env / "logs")
    intent_store.write_intents([mk_intent("2", "good", status="open", value=3), mk_intent("3", "good", status="open", value=4), mk_intent("4", "good", status="done"),
                                mk_intent("5", "good", status="open", tier=2, value=3), mk_intent("6", "other", status="open", value=3)], logs)
    ov = lo.apply([{"kind": "good", "action": "promote", "rate": 0.9, "closed": 6}], logs=logs, ledger=env / "l.md")
    got = {i["id"]: i for i in intent_store.read_jsonl(intent_store.INTENTS, logs)}
    assert got["2"]["value"] == 4 and got["3"]["value"] == 4 and got["4"]["value"] == 3 and got["5"]["value"] == 3 and got["6"]["value"] == 3
    assert ov["good"]["bonus"] == 1


def test_apply_changes_no_tier_and_records_the_numbers(env):
    logs = str(env / "logs")
    intent_store.write_intents([mk_intent("1", "bad", status="open", tier=1)], logs)
    lo.apply([{"kind": "bad", "action": "demote", "rate": 0.1, "closed": 7}], logs=logs, ledger=env / "l.md")
    assert intent_store.read_jsonl(intent_store.INTENTS, logs)[0]["tier"] == 1
    assert json.loads((env / "logs" / "intent_kind_overrides.json").read_text(encoding="utf-8"))["bad"]["closed"] == 7


def test_the_ledger_is_created_with_a_header_when_it_does_not_exist(env):
    (env / "vault" / "05 - Resources").rmdir()
    lo.apply([{"kind": "bad", "action": "demote", "rate": 0.2, "closed": 5}], logs=str(env / "logs"))
    text = (env / "logs" / "correction_ledger.md").read_text(encoding="utf-8")
    assert text.startswith("# Correction Ledger") and "kind bad" in text and "4 of 5" in text


def test_the_vault_ledger_note_is_used_when_it_exists(env):
    note = env / "vault" / "05 - Resources" / "Correction Ledger.md"
    note.write_text("# Correction Ledger\n", encoding="utf-8")
    assert lo.ledger_path() == note.resolve() or lo.ledger_path() == note
    lo.apply([{"kind": "bad", "action": "demote", "rate": 0.2, "closed": 5}], logs=str(env / "logs"))
    assert "kind bad" in note.read_text(encoding="utf-8") and not (env / "logs" / "correction_ledger.md").exists()


def test_release_removes_a_demotion(env):
    logs = str(env / "logs")
    lo.apply([{"kind": "bad", "action": "demote", "rate": 0.2, "closed": 5}], logs=logs, ledger=env / "l.md")
    assert demoted("bad", logs) is True
    assert lo.release("bad", logs) is True and demoted("bad", logs) is False
    assert lo.release("bad", logs) is False


def test_a_broken_overrides_file_reads_as_empty(env):
    (env / "logs" / "intent_kind_overrides.json").write_text("{not json", encoding="utf-8")
    assert lo._load_overrides() == {}
    (env / "logs" / "intent_kind_overrides.json").write_text("[1]", encoding="utf-8")
    assert lo._load_overrides() == {}


# ---------- the whole command ----------
def seed(env, kinds_closed, reaction):
    """Closed work items 1 to 2 hours ago, with a prompt log that began before them and `reaction` typed 5 minutes after each."""
    now = time.time()
    intents, outcomes, prompts = [], [], [{"ts": now - 5 * 3600, "text": "hello there"}]
    for n in range(kinds_closed):
        t = now - 7200 + n * 60
        intents.append(mk_intent("i%d" % n, "report_due", what="send the quarterly report"))
        outcomes.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)), "intent_id": "i%d" % n, "action": "report", "evidence": "sent", "result": "ok"})
        prompts.append({"ts": t + 300, "text": reaction})
    jsonl(env / "logs" / "intents.jsonl", intents)
    jsonl(env / "logs" / "outcomes.jsonl", outcomes)
    jsonl(env / "logs" / "user_prompts.jsonl", prompts)


def test_main_without_a_prompt_log_labels_nothing_and_says_why(env, capsys):
    jsonl(env / "logs" / "intents.jsonl", [mk_intent("a", "k")])
    jsonl(env / "logs" / "outcomes.jsonl", [mk_outcome("a", time.time() - 7200)])
    assert lo.main([]) == 0
    assert "no prompt log yet" in capsys.readouterr().out
    assert not (env / "logs" / "outcome_labels.jsonl").exists() and (env / "logs" / "learn_outcomes_last.txt").exists()


def test_main_dry_run_prints_the_table_and_writes_nothing(env, capsys):
    seed(env, 6, "that report is wrong")
    assert lo.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "report_due" in out and "6 new label(s); 1 proposal(s): demote report_due" in out
    assert not (env / "logs" / "outcome_labels.jsonl").exists() and not (env / "logs" / "alerts.txt").exists()


def test_main_default_labels_and_asks_but_changes_nothing(env, capsys):
    seed(env, 6, "that report is wrong")
    assert lo.main([]) == 0
    assert len(read_jsonl(env / "logs" / "outcome_labels.jsonl")) == 6
    assert not (env / "logs" / "intent_kind_overrides.json").exists()
    review = [i for i in read_jsonl(env / "logs" / "intents.jsonl") if i["kind"] == "intent_kind_review"]
    assert len(review) == 1 and review[0]["tier"] == 2 and "report_due" in review[0]["what"]
    assert "nothing changed" in (env / "logs" / "alerts.txt").read_text(encoding="utf-8")
    capsys.readouterr()
    lo.main([])                                                         # a second run labels nothing new and does not raise a second question
    assert len(read_jsonl(env / "logs" / "outcome_labels.jsonl")) == 6
    assert sum(1 for i in read_jsonl(env / "logs" / "intents.jsonl") if i["kind"] == "intent_kind_review") == 1


def test_main_apply_demotes_the_kind(env):
    seed(env, 6, "that report is wrong")
    assert lo.main(["--apply"]) == 0
    assert json.loads((env / "logs" / "intent_kind_overrides.json").read_text(encoding="utf-8"))["report_due"]["shadow"] is True
    assert "applied:" in (env / "logs" / "alerts.txt").read_text(encoding="utf-8")


def test_the_apply_switch_file_has_the_same_effect_as_the_flag(env):
    seed(env, 6, "that report is wrong")
    (env / "logs" / "learn_outcomes_apply_on.txt").write_text("on", encoding="utf-8")
    lo.main([])
    assert (env / "logs" / "intent_kind_overrides.json").exists()


def test_main_with_happy_reactions_proposes_a_promotion_not_a_demotion(env, capsys):
    seed(env, 6, "thanks, that works")
    lo.main(["--dry-run"])
    assert "promote report_due" in capsys.readouterr().out


def test_the_kill_switch_stops_a_real_run_but_not_a_dry_run(env, capsys):
    seed(env, 6, "that report is wrong")
    (env / "logs" / "learn_outcomes_off.txt").write_text("off", encoding="utf-8")
    assert lo.main([]) == 0 and "learn_outcomes: off" in capsys.readouterr().out
    assert not (env / "logs" / "outcome_labels.jsonl").exists()
    lo.main(["--dry-run"])
    assert "new label(s)" in capsys.readouterr().out


def test_main_release_command(env, capsys):
    lo.apply([{"kind": "bad", "action": "demote", "rate": 0.2, "closed": 5}], logs=str(env / "logs"), ledger=env / "l.md")
    assert lo.main(["--release", "bad"]) == 0 and "released bad" in capsys.readouterr().out
    assert lo.main(["--release", "bad"]) == 0 and "has no override" in capsys.readouterr().out
    assert lo.main(["--release"]) == 2


def test_main_survives_junk_in_the_logs(env):
    seed(env, 2, "ok")
    with open(env / "logs" / "outcomes.jsonl", "a", encoding="utf-8") as f:
        f.write("not json\n[1, 2]\n")
    with open(env / "logs" / "outcome_labels.jsonl", "a", encoding="utf-8") as f:
        f.write("garbage\n[3]\n")
    assert lo.main([]) == 0


def test_real_process_runs_with_a_narrow_console(tmp_path):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    e = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "learn_outcomes.py"), "--dry-run"], capture_output=True, env=e, timeout=30)
    assert r.returncode == 0 and b"no prompt log yet" in r.stdout
