# -*- coding: utf-8 -*-
"""Tests for session_start.py"""

import json
import os
import sys
from pathlib import Path

import pytest

# Add the scripts directory to path
sys.path.insert(0, str(Path(__file__).parent))

import session_start


class TestReadLines:
    def test_read_lines_happy_path(self, tmp_path):
        file = tmp_path / "test.md"
        file.write_text("line1\nline2\nline3\n")
        result = session_start.read_lines(file, 2)
        assert result == ["line1", "line2"]

    def test_read_lines_fewer_than_max(self, tmp_path):
        file = tmp_path / "test.md"
        file.write_text("line1\nline2\n")
        result = session_start.read_lines(file, 5)
        assert result == ["line1", "line2"]

    def test_read_lines_empty_file(self, tmp_path):
        file = tmp_path / "test.md"
        file.write_text("")
        result = session_start.read_lines(file, 10)
        assert result == []

    def test_read_lines_missing_file(self, tmp_path):
        file = tmp_path / "missing.md"
        result = session_start.read_lines(file, 10)
        assert result == []

    def test_read_lines_unicode(self, tmp_path):
        file = tmp_path / "test.md"
        file.write_text("line1\n日本語\n🎉\nline4\n", encoding="utf-8")
        result = session_start.read_lines(file, 10)
        assert result == ["line1", "日本語", "🎉", "line4"]

    def test_read_lines_malformed_utf8(self, tmp_path):
        file = tmp_path / "test.md"
        file.write_bytes(b"line1\n\xff\xfe\nline3\n")
        result = session_start.read_lines(file, 10)
        assert len(result) == 3
        assert result[0] == "line1"
        assert result[2] == "line3"


class TestGetVaultIndex:
    def test_get_vault_index_happy(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "VAULT-INDEX.md").write_text("\n".join(f"line{i}" for i in range(70)))
        monkeypatch.setenv("HARNESS_VAULT", str(vault))
        # Need to reload module to pick up new env
        import importlib
        importlib.reload(session_start)
        result = session_start.get_vault_index()
        assert len(result) == 60
        assert result[0] == "line0"
        assert result[59] == "line59"

    def test_get_vault_index_missing_file(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setenv("HARNESS_VAULT", str(vault))
        import importlib
        importlib.reload(session_start)
        result = session_start.get_vault_index()
        assert result == []

    def test_get_vault_index_missing_folder(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"  # doesn't exist
        monkeypatch.setenv("HARNESS_VAULT", str(vault))
        import importlib
        importlib.reload(session_start)
        result = session_start.get_vault_index()
        assert result == []


class TestGetActivePriorities:
    def test_get_active_priorities_happy(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Active Priorities.md").write_text("\n".join(f"priority{i}" for i in range(50)))
        monkeypatch.setenv("HARNESS_VAULT", str(vault))
        import importlib
        importlib.reload(session_start)
        result = session_start.get_active_priorities()
        assert len(result) == 40
        assert result[0] == "priority0"
        assert result[39] == "priority39"

    def test_get_active_priorities_missing_file(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setenv("HARNESS_VAULT", str(vault))
        import importlib
        importlib.reload(session_start)
        result = session_start.get_active_priorities()
        assert result == []


class TestGetIntentWakeLine:
    def test_get_intent_wake_line_success(self, tmp_path, monkeypatch):
        # Mock subprocess.run
        import subprocess
        original_run = subprocess.run

        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                class MockResult:
                    stdout = "NEXT abc123 tier1 value4 Do something important"
                    stderr = ""
                    returncode = 0
                return MockResult()
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = session_start.get_intent_wake_line()
        assert result == "NEXT abc123 tier1 value4 Do something important"

    def test_get_intent_wake_line_none_output(self, tmp_path, monkeypatch):
        import subprocess
        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                class MockResult:
                    stdout = "NONE nothing actionable: queue empty"
                    stderr = ""
                    returncode = 0
                return MockResult()
            return subprocess.run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = session_start.get_intent_wake_line()
        # The wake line includes NONE output per spec
        assert result == "NONE nothing actionable: queue empty"

    def test_get_intent_wake_line_error(self, tmp_path, monkeypatch):
        import subprocess
        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                raise Exception("command failed")
            return subprocess.run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = session_start.get_intent_wake_line()
        assert result == ""


class TestCountTier2Waiting:
    def test_count_tier2_waiting_happy(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        # Create intents.jsonl with some tier 2 intents
        intents_file = logs / "intents.jsonl"
        intents = [
            {"id": "1", "status": "open", "tier": 2, "value": 3},
            {"id": "2", "status": "waiting", "tier": 2, "value": 4},
            {"id": "3", "status": "open", "tier": 1, "value": 3},
            {"id": "4", "status": "done", "tier": 2, "value": 5},
        ]
        intents_file.write_text("\n".join(json.dumps(i) for i in intents))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(session_start)
        result = session_start.count_tier2_waiting()
        assert result == 2

    def test_count_tier2_waiting_missing_file(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(session_start)
        result = session_start.count_tier2_waiting()
        assert result == 0

    def test_count_tier2_waiting_malformed_json(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        intents_file = logs / "intents.jsonl"
        intents_file.write_text('{"id": "1", "status": "open", "tier": 2}\nnot valid json\n{"id": "2", "status": "open", "tier": 2}')
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(session_start)
        result = session_start.count_tier2_waiting()
        assert result == 2  # Only valid lines count


class TestGetBootReminders:
    def test_get_boot_reminders_happy(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "boot_reminders.txt").write_text("Reminder 1\nReminder 2\n")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(session_start)
        result = session_start.get_boot_reminders()
        assert result == "Reminder 1\nReminder 2"

    def test_get_boot_reminders_missing(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(session_start)
        result = session_start.get_boot_reminders()
        assert result == ""

    def test_get_boot_reminders_unicode(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "boot_reminders.txt").write_text("🎉 Unicode reminder\n日本語", encoding="utf-8")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(session_start)
        result = session_start.get_boot_reminders()
        assert "🎉" in result
        assert "日本語" in result


class TestTouchHeartbeat:
    def test_touch_heartbeat_no_error(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(session_start)
        # Should not raise
        session_start.touch_heartbeat()
        beat_file = logs / "heartbeats" / "claude_session.beat"
        assert beat_file.exists()
        content = beat_file.read_text().strip()
        assert content.replace(".", "").isdigit()


class TestBuildOutput:
    def test_build_output_all_sections(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "VAULT-INDEX.md").write_text("\n".join(f"idx{i}" for i in range(65)))
        (vault / "Active Priorities.md").write_text("\n".join(f"pri{i}" for i in range(45)))
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "boot_reminders.txt").write_text("Boot reminder")

        monkeypatch.setenv("HARNESS_VAULT", str(vault))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))

        import subprocess
        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                class MockResult:
                    stdout = "NEXT abc123 tier1 value4 Do something"
                    stderr = ""
                    returncode = 0
                return MockResult()
            return subprocess.run(cmd, **kwargs)
        monkeypatch.setattr(subprocess, "run", mock_run)

        import importlib
        importlib.reload(session_start)
        output = session_start.build_output()

        assert "VAULT-INDEX.md (first 60 lines)" in output
        assert "Active Priorities.md (first 40 lines)" in output
        assert "Intent wake line" in output
        assert "NEXT abc123 tier1 value4 Do something" in output
        assert "Tier-2 waiting for owner:" in output
        assert "boot_reminders.txt" in output
        assert "Boot reminder" in output

    def test_build_output_truncation(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        # Create a very large VAULT-INDEX.md
        (vault / "VAULT-INDEX.md").write_text("\n".join("x" * 200 for _ in range(100)))
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_VAULT", str(vault))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(session_start)
        output = session_start.build_output()
        assert len(output) <= session_start.MAX_CHARS + 20  # small allowance for truncation marker
        assert "[truncated]" in output

    def test_build_output_empty(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_VAULT", str(vault))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import subprocess
        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                class MockResult:
                    stdout = "NONE nothing actionable: queue empty"
                    stderr = ""
                    returncode = 0
                return MockResult()
            return subprocess.run(cmd, **kwargs)
        monkeypatch.setattr(subprocess, "run", mock_run)
        import importlib
        importlib.reload(session_start)
        output = session_start.build_output()
        # Should still have the tier-2 line even if everything else is empty
        assert "Tier-2 waiting for owner: 0" in output


class TestMain:
    def test_main_no_error(self, tmp_path, monkeypatch, capsys):
        vault = tmp_path / "vault"
        vault.mkdir()
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_VAULT", str(vault))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(session_start)
        session_start.main()
        captured = capsys.readouterr()
        # Should print something (at least tier-2 count)
        assert "Tier-2 waiting for owner:" in captured.out

    def test_main_never_raises(self, tmp_path, monkeypatch):
        # Even with completely broken setup, main should not raise
        monkeypatch.setenv("HARNESS_VAULT", "/nonexistent/vault")
        monkeypatch.setenv("HARNESS_LOGS", "/nonexistent/logs")
        import importlib
        importlib.reload(session_start)
        try:
            session_start.main()
        except Exception as e:
            pytest.fail(f"main() raised {e}")


# Additional edge case tests
class TestEdgeCases:
    def test_max_chars_constant(self):
        assert session_start.MAX_CHARS == 8000

    def test_paths_are_absolute(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_VAULT", str(vault))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(session_start)
        # Paths should be resolved to absolute
        assert session_start.VAULT.is_absolute()
        assert session_start.LOGS.is_absolute()

    def test_vaul_index_more_than_60_lines(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "VAULT-INDEX.md").write_text("\n".join(f"line{i}" for i in range(100)))
        monkeypatch.setenv("HARNESS_VAULT", str(vault))
        import importlib
        importlib.reload(session_start)
        result = session_start.get_vault_index()
        assert len(result) == 60

    def test_active_priorities_more_than_40_lines(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Active Priorities.md").write_text("\n".join(f"line{i}" for i in range(100)))
        monkeypatch.setenv("HARNESS_VAULT", str(vault))
        import importlib
        importlib.reload(session_start)
        result = session_start.get_active_priorities()
        assert len(result) == 40

# Added in review (2026-10-31)
def test_non_ascii_notes_are_printed_as_utf8_even_when_the_pipe_is_not(tmp_path):
    import subprocess
    root = tmp_path / "root"
    (root / "vault").mkdir(parents=True)
    (root / "vault" / "VAULT-INDEX.md").write_text("# Index – café \U0001F600\n", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_start.py")], env=env, capture_output=True)
    assert r.returncode == 0
    assert "café".encode("utf-8") in r.stdout and "\U0001F600".encode("utf-8") in r.stdout
    assert (root / "logs" / "heartbeats" / "claude_session.beat").exists()


def test_the_wake_line_with_accents_survives_a_non_utf8_pipe(tmp_path):
    import subprocess
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    here = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    subprocess.run([sys.executable, os.path.join(here, "intent_store.py"), "add", "--kind", "t", "--what", "caf\u00e9 \u2013 na\u00efve", "--tier", "1", "--value", "4", "--dedupe", "k"],
                   env=dict(env, PYTHONIOENCODING="utf-8"), check=True, capture_output=True)
    r = subprocess.run([sys.executable, os.path.join(here, "session_start.py")], env=env, capture_output=True)
    assert r.returncode == 0
    assert "NEXT ".encode() in r.stdout and "caf\u00e9 \u2013 na\u00efve".encode("utf-8") in r.stdout
