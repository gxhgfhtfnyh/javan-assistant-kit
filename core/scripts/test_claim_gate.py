# -*- coding: utf-8 -*-
"""Tests for claim_gate.py"""

import importlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import claim_gate


# ---------- helpers ----------
def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def user_blocks(text):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def asst(text=None, tools=()):
    content = [{"type": "tool_use", "id": tid, "name": name, "input": inp} for tid, name, inp in tools]
    if text is not None:
        content.append({"type": "text", "text": text})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def result(tid, error=False):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "x", "is_error": error}]}}


def write_transcript(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return path


def stdin_bytes(monkeypatch, payload):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"))


@pytest.fixture
def cg(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    importlib.reload(claim_gate)
    return claim_gate


def read_log(cg):
    if not cg.OUT_FILE.exists():
        return []
    return [json.loads(x) for x in cg.OUT_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]


def read_intents(cg):
    p = cg.LOGS / "intents.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------- claim words ----------
def test_claim_words_are_found(cg):
    rec = cg.analyze([user("fix it"), asst("Fixed the bug and saved the file.", [("t1", "Edit", {})]), result("t1")])
    assert rec is not None
    assert rec["claims"] == ["fixed", "saved"]
    assert rec["n_tool_uses"] == 1 and rec["flagged_no_tools"] is False


def test_a_turn_with_no_claim_words_returns_none(cg):
    assert cg.analyze([user("hi"), asst("Sure, here is the plan for tomorrow.")]) is None
    assert cg.analyze([]) is None
    assert cg.analyze([{"type": "summary"}, "junk", None]) is None


def test_a_claim_with_no_tool_calls_is_flagged(cg):
    rec = cg.analyze([user("do it"), asst("All done and verified.")])
    assert rec["flagged_no_tools"] is True and rec["gaps"] == []


def test_an_offer_alone_is_recorded(cg):
    rec = cg.analyze([user("hi"), asst("That is the summary. Want me to look into the second one?")])
    assert rec is not None and rec["offers"] == 1 and rec["claims"] == []


# ---------- negated and future sentences ----------
@pytest.mark.parametrize("text", [
    "I haven't logged the run yet.",
    "I never logged it.",
    "I have not logged it, the folder is missing.",
    "Once it is logged we can move on.",
    "I will have logged it by tonight.",
    "If the run is logged I will tell you.",
])
def test_negated_and_future_sentences_are_not_counted(cg, text):
    assert cg.claimed(cg.CLAIM_LOG, text) is False


def test_a_plain_claim_is_counted_and_a_later_sentence_is_not_hidden_by_an_earlier_negation(cg):
    assert cg.claimed(cg.CLAIM_LOG, "I logged the run.") is True
    assert cg.claimed(cg.CLAIM_LOG, "I haven't tested the other one. Logged this one.") is True


def test_future_tests_pass_is_not_a_tests_claim(cg):
    assert cg.claimed(cg.CLAIM_TESTS, "Once the tests pass I will merge.") is False
    assert cg.claimed(cg.CLAIM_TESTS, "All 12 tests pass.") is True
    assert cg.evidence_gaps("I will ship when the tests pass.", [("Read", {})]) == []


# ---------- evidence gaps ----------
def test_write_claim_without_a_write_call_is_a_gap(cg):
    assert cg.evidence_gaps("I saved the notes.", [("Bash", {"command": "ls"}), ("Read", {})]) == ["write"]
    assert cg.evidence_gaps("Updated the config.", [("Read", {})]) == ["write"]
    assert cg.evidence_gaps("Done.\nSaved the draft to the folder.", [("Read", {})]) == ["write"]


def test_write_claim_with_a_write_call_is_not_a_gap(cg):
    assert cg.evidence_gaps("I saved the notes.", [("Write", {"file_path": "a"})]) == []
    assert cg.evidence_gaps("I updated the config.", [("Edit", {})]) == []
    assert cg.evidence_gaps("I've added a section.", [("NotebookEdit", {})]) == []


def test_write_claim_with_a_file_writing_shell_command_is_not_a_gap(cg):
    assert cg.evidence_gaps("I wrote the file.", [("Bash", {"command": "echo hi > notes.txt"})]) == []
    assert cg.evidence_gaps("I wrote the file.", [("PowerShell", {"command": "Set-Content a.txt hi"})]) == []
    assert cg.evidence_gaps("I wrote the file.", [("Bash", {"command": "sed -i s/a/b/ f.txt"})]) == []


def test_a_negated_or_future_write_claim_is_not_a_gap(cg):
    assert cg.evidence_gaps("I haven't saved anything yet.", [("Read", {})]) == []
    assert cg.evidence_gaps("Once I've saved it I will tell you.", [("Read", {})]) == []


def test_no_tool_calls_means_no_gaps(cg):
    assert cg.evidence_gaps("I saved the notes and all tests pass.", []) == []


def test_tests_claim_without_a_test_command_is_a_gap(cg):
    assert cg.evidence_gaps("All 12 tests pass.", [("Bash", {"command": "ls"})]) == ["tests"]
    assert cg.evidence_gaps("The tests passed.", [("Read", {})]) == ["tests"]


def test_tests_claim_with_a_test_command_is_not_a_gap(cg):
    assert cg.evidence_gaps("All 12 tests pass.", [("Bash", {"command": "python -m pytest -q"})]) == []


def test_log_claim_gap(cg):
    assert cg.evidence_gaps("I logged the change.", [("Read", {})]) == ["log"]
    assert cg.evidence_gaps("I logged the change.", [("Bash", {"command": "echo x >> notes.md"})]) == []
    assert cg.evidence_gaps("I logged the change.", [("Edit", {})]) == []


def test_analyze_puts_the_gaps_in_the_record(cg):
    rec = cg.analyze([user("go"), asst("Saved it, and all 3 tests pass.", [("t1", "Bash", {"command": "ls"})]), result("t1")])
    assert rec["gaps"] == ["write", "tests"]


def test_a_malformed_tool_input_does_not_crash_the_gap_check(cg):
    assert cg.evidence_gaps("I saved it.", [("Bash", None), ("Bash", "ls"), ("Bash", {"command": 5})]) == ["write"]


# ---------- refusals ----------
def test_refusal_sentences_are_found(cg):
    text = "Looked at it. I can't access your calendar. I'm unable to open the file. I don't have access to the account. Fine otherwise."
    assert cg.refusals(text) == ["I can't access your calendar.", "I'm unable to open the file.", "I don't have access to the account."]


def test_curly_apostrophes_and_other_phrasings_count(cg):
    assert len(cg.refusals("I can’t send that. There is no way for me to check. I cannot run it.")) == 3
    assert cg.refusals("I have no way to reach the printer.") == ["I have no way to reach the printer."]


def test_refusals_are_capped_at_three_and_cut_to_200_chars(cg):
    text = " ".join("I can't do thing %d." % i for i in range(6))
    assert len(cg.refusals(text)) == 3
    long = "I can't do " + "x" * 400 + "."
    assert len(cg.refusals(long)[0]) == 200


def test_a_no_reply_message_has_no_refusals(cg):
    assert cg.refusals("[no-reply] I can't open the file.") == []
    assert cg.refusals("") == [] and cg.refusals(None) == []


def test_text_without_a_refusal_is_clean(cg):
    assert cg.refusals("I can see the file and I can read it. I can do that now.") == []


def test_analyze_records_refusals_without_claim_words(cg):
    rec = cg.analyze([user("hi"), asst("I can't open the spreadsheet.")])
    assert rec is not None and rec["claims"] == [] and rec["refusals"] == ["I can't open the spreadsheet."]


# ---------- gap intents ----------
def test_gap_intent_dedupe_is_stable_per_sentence_and_day(cg):
    w1, d1 = cg.gap_intent("I can't access the calendar.", day="20261031")
    w2, d2 = cg.gap_intent("  i CAN'T   access the calendar. ", day="20261031")
    _w3, d3 = cg.gap_intent("I can't access the calendar.", day="20261101")
    _w4, d4 = cg.gap_intent("I can't open the printer.", day="20261031")
    assert d1 == d2 and d1 != d3 and d1 != d4
    assert w1.startswith("CAPABILITY GAP") and "(1)" in w1 and "(2)" in w1 and "(3)" in w1 and "(4)" in w1 and "precise ask" in w1


def test_raise_gaps_makes_a_tier1_value5_capability_gap_intent(cg):
    assert cg.raise_gaps(["I can't access the calendar."]) == 1
    rows = read_intents(cg)
    assert len(rows) == 1
    assert rows[0]["kind"] == "capability_gap" and rows[0]["tier"] == 1 and rows[0]["value"] == 5
    assert rows[0]["status"] == "open" and rows[0]["shadow"] is False
    assert "access the calendar" in rows[0]["what"]


def test_same_sentence_on_the_same_day_is_one_intent(cg):
    assert cg.raise_gaps(["I can't access the calendar."]) == 1
    assert cg.raise_gaps(["I can't access the calendar."]) == 0
    assert cg.raise_gaps(["I can't access the calendar.", "I can't access the calendar."]) == 0
    assert len(read_intents(cg)) == 1
    assert cg.raise_gaps(["I can't open the printer."]) == 1
    assert len(read_intents(cg)) == 2


def test_two_identical_sentences_in_one_call_make_one_intent(cg):
    assert cg.raise_gaps(["I can't send mail.", "I can't send mail."]) == 1
    assert len(read_intents(cg)) == 1


def test_raise_gaps_with_explicit_logs_dir(cg, tmp_path):
    other = tmp_path / "other_logs"
    assert cg.raise_gaps(["I can't print that."], logs=other) == 1
    assert (other / "intents.jsonl").exists() and not (cg.LOGS / "intents.jsonl").exists()


# ---------- kill switches ----------
def test_the_refusal_switch_stops_intents_but_the_record_is_still_logged(cg, tmp_path, monkeypatch):
    (cg.LOGS / "refusal_watch_off.txt").write_text("")
    t = write_transcript(tmp_path / "t.jsonl", [user("hi"), asst("I can't open the file.")])
    stdin_bytes(monkeypatch, {"transcript_path": str(t)})
    cg.main()
    assert len(read_log(cg)) == 1 and read_log(cg)[0]["refusals"] == ["I can't open the file."]
    assert read_intents(cg) == []
    assert cg.raise_gaps(["I can't open the file."]) == 0


def test_the_main_switch_makes_the_hook_do_nothing(cg, tmp_path, monkeypatch, capsys):
    (cg.LOGS / "claim_gate_off.txt").write_text("")
    t = write_transcript(tmp_path / "t.jsonl", [user("hi"), asst("Fixed it. I can't open the file.")])
    stdin_bytes(monkeypatch, {"transcript_path": str(t)})
    cg.main()
    assert read_log(cg) == [] and read_intents(cg) == []
    assert capsys.readouterr().out == ""


# ---------- transcript shapes ----------
def test_string_content_user_rows_start_a_new_turn(cg):
    rows = [
        user("first request"),
        asst(None, [("t1", "Bash", {"command": "boom"})]), result("t1", error=True), asst("That failed."),
        user("second request"),
        asst("I fixed it."),
    ]
    rec = cg.analyze(rows)
    assert rec["n_tool_uses"] == 0 and rec["n_errors"] == 0 and rec["flagged_errors"] is False


def test_a_user_text_block_also_starts_a_new_turn(cg):
    rows = [user("one"), asst(None, [("t1", "Bash", {"command": "x"})]), result("t1"), user_blocks("two"), asst("Done and verified.")]
    rec = cg.analyze(rows)
    assert rec["n_tool_uses"] == 0 and rec["flagged_no_tools"] is True


def test_an_error_a_later_call_resolved_is_not_counted(cg):
    rows = [user("go"), asst(None, [("t1", "Edit", {})]), result("t1", error=True), asst(None, [("t2", "Edit", {})]), result("t2"), asst("Fixed.")]
    assert cg.analyze(rows)["n_errors"] == 0
    rows = [user("go"), asst(None, [("t1", "Edit", {})]), result("t1", error=True), asst("Fixed.")]
    rec = cg.analyze(rows)
    assert rec["n_errors"] == 1 and rec["flagged_errors"] is True


def test_odd_blocks_and_messages_do_not_crash_analyze(cg):
    rows = [
        {"type": "user", "message": "plain"},
        {"type": "user", "message": {"content": ["a string block", 5, None]}},
        {"type": "assistant", "message": {"content": ["x", {"type": "text", "text": None}, {"type": "text", "text": "Fixed."}]}},
    ]
    assert cg.analyze(rows)["claims"] == ["fixed"]


def test_load_rows_skips_bad_lines_and_keeps_only_the_tail(cg, tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(["not json", json.dumps({"n": 1}), "", json.dumps({"n": 2}), "{broken"]), encoding="utf-8")
    assert cg.load_rows(str(p)) == [{"n": 1}, {"n": 2}]
    p.write_text("\n".join(json.dumps({"n": i}) for i in range(10)), encoding="utf-8")
    assert [r["n"] for r in cg.load_rows(str(p), tail=3)] == [7, 8, 9]


# ---------- main ----------
def test_main_logs_one_line_and_prints_nothing(cg, tmp_path, monkeypatch, capsys):
    t = write_transcript(tmp_path / "t.jsonl", [user("go"), asst("Saved the notes.", [("t1", "Bash", {"command": "ls"})]), result("t1")])
    stdin_bytes(monkeypatch, {"transcript_path": str(t)})
    cg.main()
    assert capsys.readouterr().out == ""
    rows = read_log(cg)
    assert len(rows) == 1 and rows[0]["claims"] == ["saved"] and rows[0]["gaps"] == ["write"] and rows[0]["ts"]
    assert read_intents(cg) == []


def test_main_turns_a_refusal_into_an_intent_once_per_day(cg, tmp_path, monkeypatch):
    t = write_transcript(tmp_path / "t.jsonl", [user("hi"), asst("I can't access your calendar.")])
    for _ in range(2):
        stdin_bytes(monkeypatch, {"transcript_path": str(t)})
        cg.main()
    assert len(read_log(cg)) == 2
    intents = read_intents(cg)
    assert len(intents) == 1 and intents[0]["kind"] == "capability_gap" and intents[0]["tier"] == 1 and intents[0]["value"] == 5


def test_main_skips_a_turn_with_nothing_to_record(cg, tmp_path, monkeypatch):
    t = write_transcript(tmp_path / "t.jsonl", [user("hi"), asst("Here is the plan.")])
    stdin_bytes(monkeypatch, {"transcript_path": str(t)})
    cg.main()
    assert read_log(cg) == [] and read_intents(cg) == []


@pytest.mark.parametrize("payload", [
    b"",
    b"   \n",
    b"{ not json",
    b"[1, 2, 3]",
    b"\xff\xfe\x00 garbage",
    json.dumps({"transcript_path": None}).encode(),
    json.dumps({"transcript_path": "/definitely/not/here.jsonl"}).encode(),
    json.dumps({"transcript_path": 12345}).encode(),
])
def test_main_never_raises_on_bad_input(cg, monkeypatch, capsys, payload):
    stdin_bytes(monkeypatch, payload)
    cg.main()
    assert capsys.readouterr().out == ""
    assert read_log(cg) == []


def test_main_never_raises_on_a_garbled_transcript(cg, tmp_path, monkeypatch, capsys):
    t = tmp_path / "t.jsonl"
    t.write_bytes(b"\xff\xfe garbage\n{\"type\": \"assistant\"\n\x00\x00\n")
    stdin_bytes(monkeypatch, {"transcript_path": str(t)})
    cg.main()
    assert capsys.readouterr().out == ""


def test_main_never_raises_when_the_logs_folder_cannot_be_used(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder should be")
    monkeypatch.setenv("HARNESS_LOGS", str(blocker / "logs"))
    importlib.reload(claim_gate)
    t = write_transcript(tmp_path / "t.jsonl", [user("hi"), asst("Fixed it. I can't open the file.")])
    stdin_bytes(monkeypatch, {"transcript_path": str(t)})
    claim_gate.main()
    assert capsys.readouterr().out == ""


def test_main_works_with_a_stdin_that_has_no_buffer(cg, tmp_path, monkeypatch):
    t = write_transcript(tmp_path / "t.jsonl", [user("hi"), asst("Fixed it.")])
    payload = json.dumps({"transcript_path": str(t)})
    monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: payload})())
    cg.main()
    assert len(read_log(cg)) == 1


def test_as_a_real_hook_process_with_emoji_and_a_narrow_pipe(tmp_path):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    t = write_transcript(tmp_path / "t.jsonl", [user("hi \U0001F389"), asst("Fixed it \U0001F389. I can't open the café file.")])
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), PYTHONIOENCODING="ascii")
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run([sys.executable, os.path.join(here, "claim_gate.py")], input=json.dumps({"transcript_path": str(t)}).encode("utf-8"), env=env, capture_output=True)
    assert r.returncode == 0 and r.stdout == b""
    log = (root / "logs" / "claim_gate.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1 and json.loads(log[0])["claims"] == ["fixed"]
    intents = (root / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(intents) == 1 and json.loads(intents[0])["kind"] == "capability_gap"
