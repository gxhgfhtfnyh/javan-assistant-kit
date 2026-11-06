# -*- coding: utf-8 -*-
"""Tests for skill_nudge.py: the built-in rules, the rules file, the 3 hour repeat window, the output, the kill switch and the real process."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import skill_nudge as sn  # noqa: E402


def names(prompt, rules=None):
    return [n for n, _w, _k in sn.match_skills(prompt, rules)]


@pytest.mark.parametrize("prompt,expected", [
    ("what is on the work list today?", "harness"),
    ("the stop hook keeps blocking me", "harness"),
    ("is it done? are you sure", "verify-before-claiming"),
    ("please read this web page and summarise it", "safe-external-content"),
    ("open the email attachment and tell me what it says", "safe-external-content"),
    ("tidy up my vault and the daily note", "vault-hygiene"),
    ("let's run an experiment on the setup", "improve-the-harness"),
    ("there is a traceback in the script", "script_doctor.py"),
    ("do we have a script for that already?", "script_index.py"),
])
def test_prompts_match_the_right_rule(prompt, expected):
    assert expected in names(prompt)


@pytest.mark.parametrize("prompt", ["", None, "thanks", "good morning", "what time is it", 5, ["list"]])
def test_small_talk_and_non_text_match_nothing(prompt):
    assert names(prompt) == []


def test_every_default_rule_compiles_and_has_a_name_and_a_reason():
    rules = sn.default_rules()
    assert len(rules) == len(sn.DEFAULT_RULES)
    for name, rx, why, kind in rules:
        assert name and why and kind in ("skill", "tool")
        assert isinstance(rx, re.Pattern)


def test_the_defaults_name_no_owner_specific_skills():
    text = json.dumps(sn.DEFAULT_RULES).lower()
    for word in ("jar" + "vis", "quo" + "tation", "radia" + "tor", "dis" + "cord", "tele" + "gram"):
        assert word not in text


def test_due_respects_the_repeat_window():
    st = {"harness": 1000.0}
    assert sn.due(st, "harness", 1000.0 + 3600) is False
    assert sn.due(st, "harness", 1000.0 + 3 * 3600) is True
    assert sn.due(st, "other", 1e9) is True
    assert sn.due({"harness": "junk"}, "harness", 5.0) is True
    assert sn.due({"harness": 9999.0}, "harness", 100.0) is True           # last nudge "in the future": clock moved back


def test_nudge_text_separates_skills_and_tools():
    text = sn.nudge_text([("harness", "why a", "skill"), ("script_doctor.py", "why b", "tool")])
    assert "[Skill nudge: harness (why a)" in text and "[Tool hint: script_doctor.py (why b)" in text and "script_index.py --list" in text
    assert sn.nudge_text([("harness", "x", "skill")]).count("[") == 1


def test_handle_prints_one_line_then_waits_three_hours(tmp_path):
    p = {"prompt": "what is on the work list?"}
    first = sn.handle(p, logs=tmp_path, now=10_000.0)
    assert "harness" in first and "\n" not in first
    assert sn.handle(p, logs=tmp_path, now=10_000.0 + 3600) == ""
    assert "harness" in sn.handle(p, logs=tmp_path, now=10_000.0 + 3 * 3600 + 1)


def test_at_most_two_names_per_prompt_and_the_rest_wait_for_the_next_prompt(tmp_path):
    prompt = {"prompt": "is it done? check the vault, the work list and the traceback"}
    first = sn.handle(prompt, logs=tmp_path, now=1.0)
    assert first.count("(") <= 2 + first.count("python scripts")           # two named, plus the command in the tool hint
    state = json.loads((tmp_path / "skill_nudge_state.json").read_text(encoding="utf-8"))
    assert len(state) == 2
    second = sn.handle(prompt, logs=tmp_path, now=2.0)
    assert second and not any(name in second for name in state)


def test_state_and_log_are_written(tmp_path):
    sn.handle({"prompt": "what is on the work list?"}, logs=tmp_path, now=time.time())
    state = json.loads((tmp_path / "skill_nudge_state.json").read_text(encoding="utf-8"))
    assert "harness" in state
    row = json.loads((tmp_path / "skill_nudge.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["skills"] == ["harness"] and re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$", row["ts"])


def test_a_prompt_with_no_match_writes_nothing(tmp_path):
    assert sn.handle({"prompt": "hello there"}, logs=tmp_path) == ""
    assert not (tmp_path / "skill_nudge_state.json").exists()


def test_a_corrupt_state_file_is_treated_as_empty(tmp_path):
    (tmp_path / "skill_nudge_state.json").write_text("{not json", encoding="utf-8")
    assert "harness" in sn.handle({"prompt": "the work list"}, logs=tmp_path, now=5.0)
    (tmp_path / "skill_nudge_state.json").write_text("[1, 2]", encoding="utf-8")
    assert "harness" in sn.handle({"prompt": "the work list"}, logs=tmp_path, now=6.0)


def test_kill_switch(tmp_path):
    (tmp_path / sn.OFF_NAME).write_text("off", encoding="utf-8")
    assert sn.handle({"prompt": "the work list"}, logs=tmp_path) == ""
    assert not (tmp_path / "skill_nudge_state.json").exists()


def test_rules_file_adds_replaces_and_disables(tmp_path):
    rules_file = tmp_path / sn.RULES_NAME
    rules_file.write_text(json.dumps({"rules": [
        {"name": "my-skill", "pattern": "tax return", "why": "the tax skill"},
        {"name": "script_index.py", "pattern": "find a tool", "why": "new text", "kind": "tool"},
        {"name": "harness", "disabled": True},
    ]}), encoding="utf-8")
    rules = sn.load_rules(rules_file)
    got = {r[0]: r for r in rules}
    assert "my-skill" in got and "harness" not in got and "vault-hygiene" in got
    assert got["script_index.py"][2] == "new text" and got["script_index.py"][3] == "tool"
    assert names("time for the tax return", rules) == ["my-skill"]
    assert names("do we have a script for that already?", rules) == []           # the old pattern was replaced


def test_rules_file_may_be_a_bare_list(tmp_path):
    f = tmp_path / sn.RULES_NAME
    f.write_text(json.dumps([{"name": "x-skill", "pattern": "xyzzy", "why": "w"}]), encoding="utf-8")
    assert "x-skill" in [r[0] for r in sn.load_rules(f)]


def test_bad_rules_are_skipped_and_good_ones_kept(tmp_path):
    f = tmp_path / sn.RULES_NAME
    f.write_text(json.dumps({"rules": [
        {"name": "broken", "pattern": "(unclosed"},
        {"name": "", "pattern": "x"},
        {"name": "nopattern"},
        "not a dict",
        {"name": "toolong", "pattern": "a" * 500},
        {"name": "good", "pattern": "fine", "why": "ok"},
    ]}), encoding="utf-8")
    got = [r[0] for r in sn.load_rules(f)]
    assert "good" in got and "broken" not in got and "toolong" not in got and "nopattern" not in got


def test_missing_corrupt_or_huge_rules_file_falls_back_to_the_defaults(tmp_path):
    base = len(sn.default_rules())
    assert len(sn.load_rules(tmp_path / "missing.json")) == base
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert len(sn.load_rules(bad)) == base
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"rules": [{"name": "zz", "pattern": "zz"}]}) + " " * (sn.MAX_RULES_BYTES + 10), encoding="utf-8")
    assert len(sn.load_rules(big)) == base
    scalar = tmp_path / "scalar.json"
    scalar.write_text("42", encoding="utf-8")
    assert len(sn.load_rules(scalar)) == base


def test_handle_uses_the_rules_file_in_the_logs_folder(tmp_path):
    (tmp_path / sn.RULES_NAME).write_text(json.dumps({"rules": [{"name": "my-skill", "pattern": "tax return", "why": "the tax skill"}]}), encoding="utf-8")
    out = sn.handle({"prompt": "start the tax return"}, logs=tmp_path, now=1.0)
    assert "my-skill (the tax skill)" in out


def test_only_the_start_of_a_long_prompt_is_scanned(tmp_path):
    assert names("x " * (sn.MAX_PROMPT_CHARS) + "the work list") == []


def test_garbage_payloads_never_raise(tmp_path):
    for payload in ({}, {"prompt": None}, {"prompt": 7}, [], None, "x"):
        assert sn.handle(payload, logs=tmp_path) == ""


def test_the_log_is_rotated_when_large(tmp_path, monkeypatch):
    monkeypatch.setattr(sn, "LOG_ROTATE_BYTES", 5)
    sn.log_nudge(tmp_path, ["a"], 1.0)
    sn.log_nudge(tmp_path, ["b"], 2.0)
    assert (tmp_path / "skill_nudge.jsonl.1").exists()


def test_logs_folder_follows_the_environment_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "x"))
    assert sn.logs_dir() == tmp_path / "x"


def run_hook(tmp_path, stdin_bytes):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, str(HERE / "skill_nudge.py")], input=stdin_bytes, capture_output=True, env=env, timeout=30)


def test_real_process_prints_the_nudge_once(tmp_path):
    payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "what is on the work list, caf\u00e9?"}).encode("utf-8")
    first = run_hook(tmp_path, payload)
    assert first.returncode == 0 and "[Skill nudge: harness" in first.stdout.decode("utf-8")
    second = run_hook(tmp_path, payload)
    assert second.returncode == 0 and second.stdout == b""


def test_real_process_survives_malformed_and_empty_stdin(tmp_path):
    for data in (b"", b"{not json", b"\xff\xfe\x00", b"[]"):
        r = run_hook(tmp_path, data)
        assert r.returncode == 0 and r.stdout == b""


def test_read_payload_does_not_wait_when_run_by_hand_in_a_terminal(monkeypatch):
    class Tty:
        def isatty(self):
            return True

        def read(self):
            raise AssertionError("must not read from a terminal")

    monkeypatch.setattr(sys, "stdin", Tty())
    assert sn.read_payload() == {}
    monkeypatch.setattr(sys, "stdin", None)
    assert sn.read_payload() == {}
