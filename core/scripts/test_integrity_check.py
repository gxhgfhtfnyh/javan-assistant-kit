# -*- coding: utf-8 -*-
"""Tests for integrity_check.py (worker draft, reviewed and extended 2026-10-31)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import intent_store  # noqa: E402
import integrity_check as ic  # noqa: E402

CORE = ["send_gate.py", "stop_gate.py", "session_start.py", "intent_store.py", "approve.py"]


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "harness"
    for s in CORE:
        write(r / "scripts" / s, "# %s\n" % s)
    write(r / "CLAUDE.md", "# rules\n")
    write(r / ".claude" / "settings.json", json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": 'python "%s/scripts/send_gate.py"' % r.as_posix()}]}]}}))
    (r / "logs").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(r))
    monkeypatch.setenv("HARNESS_LOGS", str(r / "logs"))
    return r


def intents(root):
    return intent_store.read_jsonl("intents.jsonl", str(root / "logs"))


def test_first_run_sets_the_baseline_and_the_second_run_is_ok(root):
    r = ic.check()
    assert r["status"] == "BASELINE_SET" and r["watched"] > 0
    data = json.loads((root / "logs" / "integrity_baseline.json").read_text(encoding="utf-8"))
    assert "built" in data and len(data["hashes"]) == r["watched"]
    assert ic.check()["status"] == "OK"


def test_the_baseline_names_files_relative_to_the_folder(root):
    ic.check()
    keys = json.loads((root / "logs" / "integrity_baseline.json").read_text(encoding="utf-8"))["hashes"]
    assert "CLAUDE.md" in keys and "scripts/send_gate.py" in keys and ".claude/settings.json" in keys
    assert not any(os.path.isabs(k) for k in keys)


def test_an_edit_is_reported_as_modified(root):
    ic.check()
    write(root / "CLAUDE.md", "# rules\nnew line\n")
    r = ic.check()
    assert r["status"] == "TAMPERED" and r["modified"] == ["CLAUDE.md"] and r["missing"] == [] and r["new"] == []


def test_a_deleted_file_is_reported_as_missing(root):
    ic.check()
    (root / "scripts" / "send_gate.py").unlink()
    r = ic.check()
    assert r["status"] == "TAMPERED" and r["missing"] == ["scripts/send_gate.py"]


def test_a_new_hook_script_is_reported_as_new(root):
    ic.check()
    write(root / "scripts" / "new_hook.py", "# new\n")
    settings = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    settings["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": 'python "%s/scripts/new_hook.py"' % root.as_posix()}]}]
    write(root / ".claude" / "settings.json", json.dumps(settings))
    r = ic.check()
    assert r["status"] == "TAMPERED" and "scripts/new_hook.py" in r["new"] and ".claude/settings.json" in r["modified"]


def test_moving_the_whole_folder_is_not_a_change(root, tmp_path, monkeypatch):
    write(root / ".claude" / "settings.json", json.dumps({"hooks": {"Stop": [{"command": "python scripts/stop_gate.py"}]}}))   # relative hook paths move with the folder
    ic.check()
    moved = tmp_path / "moved"
    shutil.copytree(root, moved)
    monkeypatch.setenv("HARNESS_ROOT", str(moved))
    monkeypatch.setenv("HARNESS_LOGS", str(moved / "logs"))
    assert ic.check()["status"] == "OK"


def test_the_same_change_is_alarmed_once_but_a_different_edit_of_the_same_file_alarms_again(root):
    ic.check()
    write(root / "CLAUDE.md", "edit one\n")
    ic.check()
    ic.check()
    alerts = (root / "logs" / "alerts.txt").read_text(encoding="utf-8").splitlines()
    assert len(alerts) == 1 and "CLAUDE.md" in alerts[0] and alerts[0][:4].isdigit() and " INTEGRITY CHANGE: CLAUDE.md" in alerts[0]
    write(root / "CLAUDE.md", "edit two\n")
    ic.check()
    assert len((root / "logs" / "alerts.txt").read_text(encoding="utf-8").splitlines()) == 2
    assert len([i for i in intents(root) if i["kind"] == "integrity_alert"]) == 2


def test_the_work_item_asks_the_owner_first_and_is_never_shadow(root):
    ic.check()
    write(root / "CLAUDE.md", "changed\n")
    ic.check()
    row = [i for i in intents(root) if i["kind"] == "integrity_alert"][0]
    assert row["tier"] == 2 and row["value"] == 4 and row["shadow"] is False and "CLAUDE.md" in row["what"] and "--rebaseline" in row["what"]


def test_other_items_in_the_work_list_survive_an_alarm(root):
    other = intent_store.new_intent("homework", "something else", tier=1, value=3, dedupe="other")
    intent_store.write_intents([other], str(root / "logs"))
    ic.check()
    write(root / "CLAUDE.md", "changed\n")
    ic.check()
    assert sorted(i["kind"] for i in intents(root)) == ["homework", "integrity_alert"]


def test_repeated_checks_add_no_second_work_item(root):
    ic.check()
    write(root / "CLAUDE.md", "changed\n")
    for _ in range(4):
        ic.check()
    assert len([i for i in intents(root) if i["kind"] == "integrity_alert"]) == 1


def test_rebaseline_accepts_the_new_state_and_allows_new_alarms(root):
    ic.check()
    write(root / "CLAUDE.md", "v1\n")
    assert ic.check()["status"] == "TAMPERED"
    assert ic.check(rebaseline=True)["status"] == "BASELINE_SET"
    assert ic.check()["status"] == "OK"
    write(root / "CLAUDE.md", "v2\n")
    assert ic.check()["status"] == "TAMPERED"
    assert len([i for i in intents(root) if i["kind"] == "integrity_alert"]) == 2


def test_an_unreadable_baseline_is_an_alarm_not_a_silent_new_start(root):
    ic.check()
    write(root / "logs" / "integrity_baseline.json", "{ not json")
    write(root / "CLAUDE.md", "tampered\n")
    r = ic.check()
    assert r["status"] == "TAMPERED" and r["modified"] == ["logs/integrity_baseline.json"]
    assert (root / "logs" / "integrity_baseline.json").read_text(encoding="utf-8") == "{ not json"    # not overwritten
    assert ic.check(rebaseline=True)["status"] == "BASELINE_SET"                                       # the owner can still accept


def test_a_missing_logs_folder_is_created(root):
    shutil.rmtree(root / "logs")
    assert ic.check()["status"] == "BASELINE_SET"
    assert (root / "logs" / "integrity_baseline.json").exists()


def test_a_file_missing_at_baseline_time_is_new_when_it_appears(root):
    (root / "CLAUDE.md").unlink()
    ic.check()
    assert ic.check()["status"] == "OK"                                                               # still missing, nothing changed
    write(root / "CLAUDE.md", "now it exists\n")
    r = ic.check()
    assert r["status"] == "TAMPERED" and r["new"] == ["CLAUDE.md"]


def test_rebaselining_forgets_old_alarms_so_a_change_that_comes_back_alarms_again(root):
    ic.check()
    write(root / "CLAUDE.md", "v1\n")
    ic.check()                                      # alarm for v1
    ic.check(rebaseline=True)
    write(root / "CLAUDE.md", "# rules\n")          # back to the original text
    ic.check()                                      # alarm for the original
    ic.check(rebaseline=True)
    write(root / "CLAUDE.md", "v1\n")
    ic.check()                                      # v1 again: a fresh change after two rebaselines
    assert len((root / "logs" / "alerts.txt").read_text(encoding="utf-8").splitlines()) == 3


def test_a_hook_script_dropped_from_settings_is_reported_missing(root):
    write(root / "scripts" / "extra_hook.py", "# hook\n")
    settings = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    settings["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "python scripts/extra_hook.py"}]}]
    write(root / ".claude" / "settings.json", json.dumps(settings))
    ic.check()
    del settings["hooks"]["Stop"]
    write(root / ".claude" / "settings.json", json.dumps(settings))
    r = ic.check()
    assert r["status"] == "TAMPERED" and "scripts/extra_hook.py" in r["missing"]


def test_list_marks_a_file_that_was_missing_at_baseline_time_as_new_once_it_exists(root):
    (root / "CLAUDE.md").unlink()
    ic.check()
    write(root / "CLAUDE.md", "back\n")
    status = {f["path"]: f["status"] for f in ic.check(list_only=True)["files"]}
    assert status["CLAUDE.md"] == "NEW" and status["scripts/send_gate.py"] == "OK"


def test_each_core_script_is_watched_even_when_no_hook_names_it(root):
    write(root / ".claude" / "settings.json", "{}")
    watched = ic._critical_files()
    for s in CORE:
        assert "scripts/" + s in watched


def test_core_scripts_settings_and_claude_md_are_always_watched(root):
    watched = ic._critical_files()
    for name in ["CLAUDE.md", ".claude/settings.json"] + ["scripts/" + s for s in CORE] + ["scripts/job_runner.py", "scripts/jobs.json", "scripts/integrity_check.py"]:
        assert name in watched


# ---------- reading hook commands ----------
def test_a_malformed_settings_file_is_survived(root):
    write(root / ".claude" / "settings.json", "{ not valid json }")
    assert ic._hook_paths_from_settings() == []
    assert ic.check()["status"] == "BASELINE_SET"


def test_a_settings_file_with_a_byte_order_mark_is_read(root):
    (root / ".claude" / "settings.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"hooks": {"Stop": [{"command": "python scripts/stop_gate.py"}]}}).encode("utf-8"))
    assert ic._hook_paths_from_settings() == ["scripts/stop_gate.py"]


@pytest.mark.parametrize("command,expected", [
    ('python scripts/send_gate.py', ["scripts/send_gate.py"]),
    ('python "scripts/send_gate.py" --arg "value with spaces"', ["scripts/send_gate.py"]),
    ("python 'scripts/send_gate.py'", ["scripts/send_gate.py"]),
    (r'python "C:\Users\Ann Lee\assistant\scripts\send_gate.py"', [r"C:\Users\Ann Lee\assistant\scripts\send_gate.py"]),
    ('python "/home/ann lee/assistant/scripts/send_gate.py" check', ["/home/ann lee/assistant/scripts/send_gate.py"]),
    ('powershell -File scripts/hook.ps1', ["scripts/hook.ps1"]),
    ('cmd /c scripts/hook.bat', ["scripts/hook.bat"]),
    ('sh scripts/hook.sh', ["scripts/hook.sh"]),
    ('python -m pytest', []),
    ('', []),
])
def test_script_paths_from_a_hook_command(command, expected):
    assert ic.script_paths(command) == expected


def test_a_hook_path_with_a_space_in_it_is_watched_whole(tmp_path, monkeypatch):
    r = tmp_path / "Ann Lee" / "assistant"
    for s in CORE:
        write(r / "scripts" / s, "# %s\n" % s)
    write(r / "CLAUDE.md", "x")
    write(r / "scripts" / "extra_hook.py", "# hook\n")
    write(r / ".claude" / "settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": 'python "%s/scripts/extra_hook.py"' % r.as_posix()}]}]}}))
    monkeypatch.setenv("HARNESS_ROOT", str(r))
    monkeypatch.setenv("HARNESS_LOGS", str(r / "logs"))
    assert "scripts/extra_hook.py" in ic._critical_files()
    ic.check()
    write(r / "scripts" / "extra_hook.py", "# hook, edited\n")
    assert ic.check()["modified"] == ["scripts/extra_hook.py"]


def test_hook_commands_found_in_flat_and_nested_shapes():
    flat = {"hooks": {"PreToolUse": [{"matcher": "x", "command": "a.py"}]}}
    nested = {"hooks": {"PreToolUse": [{"matcher": "x", "hooks": [{"type": "command", "command": "b.py"}]}]}}
    assert ic.hook_commands(flat) == ["a.py"] and ic.hook_commands(nested) == ["b.py"]


# ---------- the command line ----------
def run_cli(root, *args):
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"))
    return subprocess.run([sys.executable, os.path.join(HERE, "integrity_check.py"), *args], capture_output=True, text=True, env=env)


def test_cli_exit_code_is_1_when_tampered_and_0_with_job(root):
    run_cli(root)
    write(root / "CLAUDE.md", "changed\n")
    assert run_cli(root).returncode == 1
    r = run_cli(root, "--job")
    assert r.returncode == 0 and "TAMPERED" in r.stdout


def test_cli_list_and_rebaseline(root):
    assert run_cli(root, "--rebaseline").returncode == 0
    out = run_cli(root, "--list").stdout
    assert "Watching" in out and "OK" in out and "CLAUDE.md" in out
    write(root / "CLAUDE.md", "changed\n")
    assert "MODIFIED" in run_cli(root, "--list").stdout


def test_the_check_never_changes_a_watched_file(root):
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and "logs" not in p.parts}
    ic.check()
    write(root / "CLAUDE.md", "edited\n")
    before[root / "CLAUDE.md"] = (root / "CLAUDE.md").read_bytes()
    ic.check()
    assert {p: p.read_bytes() for p in before} == before


def test_unicode_content_hashes_fine(root):
    ic.check()
    write(root / "CLAUDE.md", "café ☕ 🎉\n")
    assert ic.check()["modified"] == ["CLAUDE.md"]
