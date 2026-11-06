# -*- coding: utf-8 -*-
"""Tests for approve.py (the owner's side of the send gate). A temp folder stands in for logs/."""
import io
import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approve as ap  # noqa: E402
import send_gate as sg  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(autouse=True)
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    return tmp_path / "logs"


def held(command):
    verdict, reason = sg.decide("Bash", {"command": command})
    assert verdict == "deny"
    return reason.split("python scripts/approve.py ")[1].split()[0]


def test_approving_by_code_lets_exactly_that_command_through_and_prints_what_it_approved(capsys):
    code = held("git push origin main")
    assert ap.main([code]) == 0
    out = capsys.readouterr().out
    assert "approved for 30 minutes" in out and "git push origin main" in out
    assert sg.decide("Bash", {"command": "git  push   origin main"}) == ("allow", "approved")
    assert sg.decide("Bash", {"command": "git push origin other"})[0] == "deny"


def test_the_approval_records_the_full_hash_and_an_expiry_in_the_future(logs):
    code = held("git push")
    ap.main([code, "--minutes", "5"])
    row = json.loads((logs / "approvals.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["sha"] == sg.sha256("git push") and row["preview"] == "git push"
    assert 200 < (sg._parse_time(row["expires"]) - sg._parse_time(row["ts"])).total_seconds() <= 300


def test_an_uppercase_code_works(capsys):
    assert ap.main([held("git push").upper()]) == 0


@pytest.mark.parametrize("code", ["", "abc", "zzzzzzzz", "12 34"])
def test_a_code_that_is_not_a_code_is_refused(code, capsys):
    held("git push")
    assert ap.main([code] if code else []) == 1
    assert capsys.readouterr().out


def test_a_one_character_code_cannot_approve_anything_even_when_a_held_hash_starts_with_it(logs, capsys):
    logs.mkdir()
    (logs / "pending_approvals.jsonl").write_text(json.dumps({"ts": time.time(), "sha": "a" + "0" * 63, "tool": "Bash", "text": "git push"}) + "\n", encoding="utf-8")
    assert ap.main(["a"]) == 1 and "not a code" in capsys.readouterr().out
    assert ap.main(["a0000"]) == 1                                      # 5 characters: still too short
    assert not (logs / "approvals.jsonl").exists()


def test_an_unknown_code_is_refused_and_nothing_is_approved(logs, capsys):
    held("git push")
    assert ap.main(["deadbeef00"]) == 1 and "no held send matches" in capsys.readouterr().out
    assert not (logs / "approvals.jsonl").exists()


def test_a_code_from_more_than_a_day_ago_is_refused(logs):
    logs.mkdir()
    sha = sg.sha256("git push")
    (logs / "pending_approvals.jsonl").write_text(json.dumps({"ts": time.time() - 90000, "sha": sha, "tool": "Bash", "text": "git push"}) + "\n", encoding="utf-8")
    assert ap.main([sha[:10]]) == 1


def test_a_code_that_matches_two_different_held_texts_is_refused(logs, capsys):
    logs.mkdir()
    rows = [{"ts": time.time(), "sha": "ab12cd" + "0" * 58, "tool": "Bash", "text": "one"}, {"ts": time.time(), "sha": "ab12cd" + "1" * 58, "tool": "Bash", "text": "two"}]
    (logs / "pending_approvals.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert ap.main(["ab12cd"]) == 1 and "more than one" in capsys.readouterr().out
    assert ap.main(["ab12cd0"]) == 0


def test_the_same_text_held_twice_is_one_candidate(logs):
    held("git push")
    held("git push")
    assert ap.main([sg.sha256("git push")[:10]]) == 0


def test_approving_ahead_of_time_with_the_exact_text(capsys):
    assert ap.main(["--text", "  curl  -X POST https://example.com -d x "]) == 0
    assert sg.decide("Bash", {"command": "curl -X POST https://example.com -d x"}) == ("allow", "approved")
    assert "curl -X POST https://example.com -d x" in capsys.readouterr().out


def test_empty_text_is_refused(capsys):
    assert ap.main(["--text", "   "]) == 1 and "empty" in capsys.readouterr().out


@pytest.mark.parametrize("minutes", ["0", "-5", "1441", "abc", "1.5"])
def test_bad_minutes_are_refused_and_nothing_is_recorded(minutes, logs, capsys):
    code = held("git push")
    assert ap.main([code, "--minutes", minutes]) == 1
    assert not (logs / "approvals.jsonl").exists()


def test_the_longest_allowed_approval_is_a_day(logs):
    assert ap.main([held("git push"), "--minutes", "1440"]) == 0


def test_unknown_options_are_refused(capsys):
    assert ap.main(["--frobnicate"]) == 1 and "unknown option" in capsys.readouterr().out


def test_no_arguments_prints_usage(capsys):
    assert ap.main([]) == 1 and "usage" in capsys.readouterr().out


def test_approving_another_tools_call_uses_its_name_and_input():
    tin = {"channel": "C1", "text": "hi"}
    verdict, reason = sg.decide("mcp__slack__post_message", tin)
    code = reason.split("python scripts/approve.py ")[1].split()[0]
    assert ap.main([code]) == 0
    assert sg.decide("mcp__slack__post_message", tin)[0] == "allow"


def test_the_whole_flow_as_real_processes(tmp_path):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), PYTHONIOENCODING="ascii")
    hook = lambda cmd: subprocess.run([sys.executable, os.path.join(HERE, "send_gate.py")], input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}), capture_output=True, text=True, env=env)
    first = hook("git push origin main")
    reason = json.loads(first.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    code = reason.split("python scripts/approve.py ")[1].split()[0]
    owner = subprocess.run([sys.executable, os.path.join(HERE, "approve.py"), code], capture_output=True, text=True, env=env)
    assert owner.returncode == 0 and "git push origin main" in owner.stdout
    assert hook("git push origin main").stdout == ""                       # now allowed
    assert json.loads(hook("git push origin other").stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    # and the assistant cannot run the approval itself
    assert json.loads(hook("python scripts/approve.py " + code).stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
