# -*- coding: utf-8 -*-
"""Tests for voice_hook: the reminder line, and the hook end to end on fake logs."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import voice_hook as vh  # noqa: E402
from voice_stats import features  # noqa: E402


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def run_hook(logs):
    env = dict(os.environ, HARNESS_LOGS=str(logs))
    p = subprocess.run([sys.executable, "-B", os.path.join(HERE, "voice_hook.py")], capture_output=True, text=True, env=env, timeout=30, encoding="utf-8")
    return p.returncode, p.stdout.strip()


def test_long_replies_get_a_length_fix_and_examples():
    mine = [features("word " * 70), features("word " * 80)]
    line = vh.build_line(12, mine, ["ok done", "yeah got it"])
    assert "averaged 75 words" in line and "about 12" in line and "Sound like" in line and '"ok done"' in line and "lowercase lines" in line


def test_lists_and_report_openers_are_named():
    mine = [features("- a\n- b"), features("Certainly. here you go")]
    line = vh.build_line(12, mine, [])
    assert "had list lines" in line and "opened like a report" in line and "Sound like" not in line


def test_on_voice_messages_get_a_keep_it_note():
    line = vh.build_line(12, [features("yeah done, all good"), features("ok on it")], ["x"])
    assert "on voice, keep it" in line and "Sound like" not in line


def test_no_recent_messages_means_no_line():
    assert vh.build_line(12, [], ["x"]) == ""


def test_the_register_is_copied_from_the_owner_not_assumed():
    mine = [features("word " * 70)]
    formal = vh.build_line(40, mine + mine, [], his_lower=0.4, his_lists=True)
    assert "lowercase" not in formal and "no lists" not in formal
    casual = vh.build_line(8, mine, [], his_lower=1.0, his_lists=False)
    assert "lowercase" in casual and "no lists" in casual


def test_lists_are_not_flagged_when_the_owner_writes_lists_too():
    line = vh.build_line(12, [features("- a\n- b")], [], his_lists=True)
    assert "list lines" not in line


def test_median():
    assert vh.median([3, 1, 2]) == 2 and vh.median([1, 2, 3, 4]) == 2.5 and vh.median([]) == 0


def test_hook_prints_one_line_from_the_logs_and_ignores_other_authors(tmp_path):
    (tmp_path / "discord_config.json").write_text(json.dumps({"owner_id": 7}), encoding="utf-8")
    write_jsonl(tmp_path / "discord_incoming.jsonl", [
        {"author_id": 7, "source": "dm", "text": "hey you there"},
        {"author_id": 7, "source": "dm", "text": "what happened to that"},
        {"author_id": 99, "source": "dm", "text": "one two three four five six seven eight nine ten eleven twelve thirteen fourteen"},
    ])
    write_jsonl(tmp_path / "discord_sent.jsonl", [{"text": "word " * 90}, {"text": "ok that works fine, I looked at it and it is done"}])
    code, out = run_hook(tmp_path)
    assert code == 0 and out.startswith("[VOICE] your owner texts about 4 words, lowercase, no lists.") and "averaged" in out
    assert len(out.splitlines()) == 1


def test_hook_is_silent_without_logs_without_an_owner_and_with_the_kill_switch(tmp_path):
    assert run_hook(tmp_path) == (0, "")
    (tmp_path / "discord_config.json").write_text(json.dumps({"owner_id": 7}), encoding="utf-8")
    assert run_hook(tmp_path) == (0, "")
    write_jsonl(tmp_path / "discord_incoming.jsonl", [{"author_id": 7, "source": "dm", "text": "hi"}])
    write_jsonl(tmp_path / "discord_sent.jsonl", [{"text": "hello there friend"}])
    assert run_hook(tmp_path)[1].startswith("[VOICE]")
    (tmp_path / "voice_hook_off.txt").write_text("off", encoding="utf-8")
    assert run_hook(tmp_path) == (0, "")


def test_hook_survives_garbage_in_the_logs(tmp_path):
    (tmp_path / "discord_config.json").write_text(json.dumps({"owner_id": 7}), encoding="utf-8")
    (tmp_path / "discord_incoming.jsonl").write_bytes(b"\xff\xfe not json\n{\"author_id\": 7, \"source\": \"dm\", \"text\": 5}\n")
    (tmp_path / "discord_sent.jsonl").write_text("[]\n", encoding="utf-8")
    assert run_hook(tmp_path) == (0, "")
