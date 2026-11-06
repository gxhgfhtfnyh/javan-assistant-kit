# -*- coding: utf-8 -*-
"""Tests for vault_checkpoint_hook.py: which paths count, the 10 minute debounce, the JSON, the kill switch and the real process."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vault_checkpoint_hook as h  # noqa: E402


@pytest.fixture
def area(tmp_path):
    (tmp_path / "vault" / "05 - Resources").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    (tmp_path / "vault" / "05 - Resources" / "Note.md").write_text("x", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("boot", encoding="utf-8")
    return tmp_path


def call(area, path, tool="Write", now=None):
    payload = {"tool_name": tool, "tool_input": {"file_path": str(path)}}
    return h.handle(payload, logs=area / "logs", vault=area / "vault", boot_file=area / "CLAUDE.md", now=now)


def test_a_vault_edit_fires_the_reminder_as_additional_context(area):
    out = call(area, area / "vault" / "05 - Resources" / "Note.md")
    data = json.loads(out)["hookSpecificOutput"]
    assert data["hookEventName"] == "PostToolUse" and "vault-checkpoint reminder" in data["additionalContext"]
    assert "index" in data["additionalContext"] and "read the change back" in data["additionalContext"]


def test_the_message_names_no_owner_specific_things():
    low = h.MESSAGE.lower()
    for word in ("jar" + "vis", "art" + "hoven", "lo" + "om"):
        assert word not in low


def test_the_boot_file_counts(area):
    assert call(area, area / "CLAUDE.md") != ""


def test_a_new_note_that_does_not_exist_yet_still_counts(area):
    assert call(area, area / "vault" / "02 - Projects" / "New.md") != ""


def test_files_outside_the_vault_are_ignored(area):
    assert call(area, area / "scripts" / "tool.py") == ""
    assert call(area, area.parent / "other.md") == ""
    assert call(area, area / "logs" / "alerts.txt") == ""


def test_a_sibling_folder_that_shares_the_vault_prefix_is_not_the_vault(area):
    assert call(area, area / "vault-backup" / "Note.md") == ""


def test_obsidian_settings_do_not_count(area):
    assert call(area, area / "vault" / ".obsidian" / "workspace.json") == ""


def test_the_vault_folder_itself_is_not_a_file_edit(area):
    assert call(area, area / "vault") == ""


def test_a_second_edit_inside_ten_minutes_is_silent_and_a_later_one_fires_again(area):
    note = area / "vault" / "05 - Resources" / "Note.md"
    t0 = 1_000_000.0
    assert call(area, note, now=t0) != ""
    assert call(area, note, now=t0 + 9 * 60) == ""
    assert call(area, note, now=t0 + 11 * 60) != ""


def test_a_silent_edit_does_not_extend_the_cooldown(area):
    note = area / "vault" / "05 - Resources" / "Note.md"
    t0 = 1_000_000.0
    call(area, note, now=t0)
    call(area, note, now=t0 + 9 * 60)           # silent
    assert call(area, note, now=t0 + 10 * 60 + 1) != ""


def test_an_edit_outside_the_vault_does_not_use_up_the_cooldown(area):
    call(area, area / "scripts" / "x.py", now=5.0)
    assert call(area, area / "vault" / "05 - Resources" / "Note.md", now=6.0) != ""


def test_cooldown_active_edges(tmp_path):
    f = tmp_path / "last.txt"
    assert h.cooldown_active(f, now=100.0) is False                      # missing file
    f.write_text("not a number", encoding="utf-8")
    assert h.cooldown_active(f, now=100.0) is False                      # unreadable
    f.write_text("1000", encoding="utf-8")
    assert h.cooldown_active(f, now=1000.0 + 599) is True
    assert h.cooldown_active(f, now=1000.0 + 600) is False
    assert h.cooldown_active(f, now=500.0) is False                      # future-dated (clock moved back): no cooldown


def test_record_reminder_writes_atomically_and_leaves_no_temp_files(tmp_path):
    f = tmp_path / "logs" / "last.txt"
    assert h.record_reminder(f, now=42.0) is True
    assert float(f.read_text(encoding="utf-8")) == 42.0
    assert [p.name for p in f.parent.iterdir()] == ["last.txt"]


def test_a_cooldown_file_that_cannot_be_written_does_not_stop_the_reminder(area):
    note = area / "vault" / "05 - Resources" / "Note.md"
    (area / "logs").rmdir()
    (area / "logs").write_text("a file where the logs folder should be", encoding="utf-8")
    out = h.handle({"tool_name": "Write", "tool_input": {"file_path": str(note)}}, logs=area / "logs", vault=area / "vault", boot_file=area / "CLAUDE.md")
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == h.MESSAGE      # the reminder still comes; only the debounce could not be saved


def test_other_tools_are_ignored(area):
    assert call(area, area / "vault" / "05 - Resources" / "Note.md", tool="Bash") == ""
    assert call(area, area / "vault" / "05 - Resources" / "Note.md", tool="Read") == ""


def test_multiedit_counts(area):
    assert call(area, area / "vault" / "05 - Resources" / "Note.md", tool="MultiEdit") != ""


def test_kill_switch(area):
    (area / "logs" / h.OFF_NAME).write_text("off", encoding="utf-8")
    assert call(area, area / "CLAUDE.md") == ""
    assert not (area / "logs" / h.STATE_NAME).exists()


def test_garbage_payloads_never_raise(area):
    for payload in ({}, {"tool_name": "Write"}, {"tool_input": 5}, {"tool_input": {"file_path": 7}}, {"tool_input": {"file_path": ""}}, [], None, "x"):
        assert h.handle(payload, logs=area / "logs", vault=area / "vault", boot_file=area / "CLAUDE.md") == ""


def test_paths_follow_the_environment_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.delenv("HARNESS_VAULT", raising=False)
    assert h.vault_dir() == tmp_path / "vault"
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "notes"))
    assert h.vault_dir() == tmp_path / "notes"
    assert h.is_checkpoint_path(str(tmp_path / "notes" / "A.md")) is True
    assert h.is_checkpoint_path(str(tmp_path / "CLAUDE.md")) is True


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_a_symlink_into_the_vault_is_resolved(area):
    link = area / "link.md"
    link.symlink_to(area / "vault" / "05 - Resources" / "Note.md")
    assert call(area, link) != ""


def run_hook(tmp_path, stdin_bytes):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, str(HERE / "vault_checkpoint_hook.py")], input=stdin_bytes, capture_output=True, env=env, timeout=30)


def test_real_process_fires_once_then_stays_quiet(area):
    note = area / "vault" / "05 - Resources" / "Café.md"
    payload = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": str(note)}}).encode("utf-8")
    first = run_hook(area, payload)
    assert first.returncode == 0
    assert "vault-checkpoint reminder" in json.loads(first.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
    assert (area / "logs" / "vault_checkpoint_last.txt").exists()
    second = run_hook(area, payload)
    assert second.returncode == 0 and second.stdout == b""


def test_real_process_survives_malformed_and_empty_stdin(area):
    for data in (b"", b"{not json", b"\xff\xfe\x00", b"[]"):
        r = run_hook(area, data)
        assert r.returncode == 0 and r.stdout == b""


def test_read_payload_does_not_wait_when_run_by_hand_in_a_terminal(monkeypatch):
    class Tty:
        def isatty(self):
            return True

        def read(self):
            raise AssertionError("must not read from a terminal")

    monkeypatch.setattr(sys, "stdin", Tty())
    assert h.read_payload() == {}
    monkeypatch.setattr(sys, "stdin", None)
    assert h.read_payload() == {}
