# -*- coding: utf-8 -*-
"""Tests for prompt_log_hook.py: redaction, the row, the log file, trimming, the kill switch and the real process."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import prompt_log_hook as h  # noqa: E402


@pytest.fixture
def logs(tmp_path, monkeypatch):
    lg = tmp_path / "logs"
    lg.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(lg))
    return lg


def rows_of(lg):
    return [json.loads(x) for x in (lg / "user_prompts.jsonl").read_text(encoding="utf-8").splitlines()]


def test_redact_removes_a_password_and_what_follows_it():
    out = h.redact("my password is hunter2 please keep going")
    assert "hunter2" not in out and "[redacted]" in out and "keep going" in out


def test_redact_removes_well_known_key_formats():
    key = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
    assert key not in h.redact("use " + key + " for the push")
    assert "sk-" + "abcdefghijklmnopqrstu" not in h.redact("key sk-" + "abcdefghijklmnopqrstu here")


def test_redact_removes_long_unbroken_key_like_runs_but_not_ordinary_words():
    long_run = "Zm9vYmFyYmF6cXV4" * 3
    assert long_run not in h.redact("see " + long_run)
    assert h.redact("please fix the intent store tests") == "please fix the intent store tests"


def test_redact_handles_none_and_non_strings():
    assert h.redact(None) == "" and h.redact(5) == "5"


def test_make_row_collapses_white_space_and_caps_the_length():
    row = h.make_row({"prompt": "line one\n\n  line   two " + "x " * 600}, now=100.0)
    assert row["ts"] == 100.0 and "\n" not in row["text"] and len(row["text"]) <= h.TEXT_CAP
    assert row["text"].startswith("line one line two")


@pytest.mark.parametrize("payload", [None, [], "text", {}, {"prompt": ""}, {"prompt": "   \n"}, {"prompt": 5}, {"prompt": None}])
def test_make_row_gives_nothing_for_a_payload_without_a_real_prompt(payload):
    assert h.make_row(payload) is None


def test_record_appends_one_line_per_prompt_and_read_prompts_returns_them_in_order(logs):
    assert h.record({"prompt": "first"}, now=10.0) and h.record({"prompt": "second"}, now=20.0)
    assert [r["text"] for r in h.read_prompts()] == ["first", "second"]
    assert [r["text"] for r in h.read_prompts(since=15.0)] == ["second"]
    assert len(rows_of(logs)) == 2


def test_record_creates_the_logs_folder(tmp_path, monkeypatch):
    lg = tmp_path / "new" / "logs"
    monkeypatch.setenv("HARNESS_LOGS", str(lg))
    assert h.record({"prompt": "hello"}) is True and (lg / "user_prompts.jsonl").exists()


def test_the_kill_switch_stops_recording(logs):
    (logs / "prompt_log_off.txt").write_text("off", encoding="utf-8")
    assert h.record({"prompt": "hello"}) is False
    assert not (logs / "user_prompts.jsonl").exists()


def test_unicode_is_kept(logs):
    h.record({"prompt": "café — 日本語"})
    assert h.read_prompts()[0]["text"] == "café — 日本語"


def test_read_prompts_skips_bad_lines_and_rows_without_a_number_ts(logs):
    (logs / "user_prompts.jsonl").write_text('not json\n{"ts": "x", "text": "a"}\n{"ts": true, "text": "b"}\n[1]\n{"ts": 5, "text": "ok"}\n\n', encoding="utf-8")
    assert h.read_prompts() == [{"ts": 5.0, "text": "ok"}]


def test_read_prompts_on_a_missing_log_is_empty(logs):
    assert h.read_prompts() == []


def test_the_log_is_trimmed_to_the_newest_rows_when_it_gets_big(logs, monkeypatch):
    monkeypatch.setattr(h, "MAX_BYTES", 2000)
    monkeypatch.setattr(h, "KEEP_ROWS", 5)
    for i in range(60):
        h.record({"prompt": "message number %d with some padding text" % i}, now=float(i + 1))
    texts = [r["text"] for r in h.read_prompts()]
    assert 1 <= len(texts) <= 60 and texts[-1].endswith("padding text") and "number 59" in texts[-1]
    assert (logs / "user_prompts.jsonl").stat().st_size < 2000 + 200


def test_trim_rows_keeps_the_newest():
    assert h.trim_rows([1, 2, 3, 4], keep=2) == [3, 4] and h.trim_rows([1], keep=0) == []


def test_record_never_raises_when_the_logs_path_is_a_file(tmp_path, monkeypatch):
    bad = tmp_path / "logs"
    bad.write_text("i am a file", encoding="utf-8")
    monkeypatch.setenv("HARNESS_LOGS", str(bad))
    assert h.record({"prompt": "hello"}) is False


def test_read_stdin_takes_utf8_bytes_with_or_without_a_bom(monkeypatch):
    import io

    class Fake:
        def __init__(self, data):
            self.buffer = io.BytesIO(data)
    monkeypatch.setattr(sys, "stdin", Fake(b'\xef\xbb\xbf{"prompt": "caf\xc3\xa9"}'))
    assert h.read_stdin() == {"prompt": "café"}
    monkeypatch.setattr(sys, "stdin", Fake(b"[1, 2]"))
    assert h.read_stdin() == {}


def run_hook(root, stdin_bytes):
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, str(HERE / "prompt_log_hook.py")], input=stdin_bytes, capture_output=True, env=env, timeout=30)


def test_real_process_logs_a_prompt_prints_nothing_and_exits_zero(tmp_path):
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": "that is wrong, café again"}
    r = run_hook(tmp_path, json.dumps(payload).encode("utf-8"))
    assert r.returncode == 0 and r.stdout == b""
    assert rows_of(tmp_path / "logs")[0]["text"] == "that is wrong, café again"


def test_real_process_survives_empty_and_malformed_input(tmp_path):
    for data in (b"", b"{not json", b"\xff\xfe\x00", b"[]", b'{"prompt": 7}'):
        r = run_hook(tmp_path, data)
        assert r.returncode == 0 and r.stdout == b""
    assert not (tmp_path / "logs" / "user_prompts.jsonl").exists()
