"""Tests added in review (2026-10-31): the interpreter written into the hooks and notes, and the verify mode the setup session uses instead of reading the new folder."""
import json
import os
import shutil
import sys

import pytest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)
import generate as G  # noqa: E402

ANSWERS = {"name": "Ann Lee", "assistant_name": "Penny"}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("built") / "My Assistant"
    G.generate(KIT, ANSWERS, out)
    return out


@pytest.fixture
def fresh(tmp_path):
    out = tmp_path / "copy"
    shutil.copytree(os.fspath(_BUILT[0]), out)
    return out


_BUILT = []


@pytest.fixture(autouse=True)
def _remember(built):
    if not _BUILT:
        _BUILT.append(built)


# ---------- the interpreter ----------
def test_the_hooks_start_with_the_full_path_of_the_python_that_built_the_folder(built):
    data = json.loads((built / ".claude" / "settings.json").read_text(encoding="utf-8"))
    cmds = [h["command"] for entries in data["hooks"].values() for e in entries for h in e["hooks"]]
    exe = sys.executable.replace("\\", "/")
    names = ("session_start.py", "prompt_context_hook.py", "recall_hook.py", "send_gate.py", "activity_log_hook.py", "injection_prescan_hook.py",
             "doctor_edit_hook.py", "vault_checkpoint_hook.py", "tool_heartbeat_hook.py", "similar_check.py", "activity_log_hook.py",
             "stop_gate.py", "keep_going.py", "claim_gate.py", "handoff_write.py")
    assert len(cmds) == len(names)
    for cmd, name in zip(cmds, names):
        assert cmd == '"%s" "%s/scripts/%s"' % (exe, built.resolve().as_posix(), name)


def test_the_boot_file_tells_the_assistant_which_python_command_to_type(built):
    text = (built / "CLAUDE.md").read_text(encoding="utf-8")
    plain = G.python_values()["python_plain"]
    assert "that command is `%s`" % plain in text and sys.executable.replace("\\", "/") in text


@pytest.mark.parametrize("platform,have,expected", [
    ("win32", {"python"}, "python"),
    ("win32", {"py"}, "py -3"),
    ("win32", {"python", "py"}, "python"),
    ("win32", set(), "python"),
    ("darwin", {"python3"}, "python3"),
    ("linux", {"python3", "python"}, "python3"),
    ("linux", {"python"}, "python"),
    ("linux", set(), "python3"),
])
def test_the_command_the_notes_use_depends_on_what_the_computer_has(monkeypatch, platform, have, expected):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/" + name if name in have else None)
    assert G.python_values()["python_plain"] == expected


# ---------- verify ----------
def test_a_good_folder_verifies_and_says_what_it_checked(fresh):
    ok, lines = G.verify(fresh, ANSWERS)
    assert ok, lines
    text = "\n".join(lines)
    assert "the boot file names you (Ann Lee)" in text and "the boot file names the assistant (Penny)" in text and "no unfilled {{placeholder}} anywhere" in text
    assert "scripts, each with a test file" in text and "15 hook commands wired" in text and not any(l.startswith("FAILED") for l in lines)


def test_a_missing_boot_file_fails(fresh):
    (fresh / "CLAUDE.md").unlink()
    ok, lines = G.verify(fresh, ANSWERS)
    assert not ok and "FAILED: boot file CLAUDE.md exists" in lines


def test_the_wrong_name_in_the_boot_file_fails(fresh):
    ok, lines = G.verify(fresh, {"name": "Somebody Else"})
    assert not ok and "FAILED: the boot file names you (Somebody Else)" in lines


def test_a_leftover_placeholder_fails_and_is_named(fresh):
    p = fresh / "vault" / "Active Priorities.md"
    p.write_text(p.read_text(encoding="utf-8") + "\nhello {{name}}\n", encoding="utf-8")
    ok, lines = G.verify(fresh, ANSWERS)
    assert not ok and any(l.startswith("FAILED: unfilled placeholders in:") and "Active Priorities.md" in l for l in lines)


def test_a_script_without_a_test_file_fails(fresh):
    (fresh / "scripts" / "test_vault_check.py").unlink()
    ok, lines = G.verify(fresh, ANSWERS)
    assert not ok and any(l.startswith("FAILED: no test file for:") and "vault_check.py" in l for l in lines)


def test_a_hook_whose_program_does_not_exist_fails(fresh):
    p = fresh / ".claude" / "settings.json"
    p.write_text(p.read_text(encoding="utf-8").replace(sys.executable.replace("\\", "/"), "C:/nowhere/python-gone.exe"), encoding="utf-8")
    ok, lines = G.verify(fresh, ANSWERS)
    assert not ok and any("the hooks name a program that does not exist" in l and "python-gone.exe" in l for l in lines)


def test_a_damaged_settings_file_fails(fresh):
    (fresh / ".claude" / "settings.json").write_text("{ nope", encoding="utf-8")
    ok, lines = G.verify(fresh, ANSWERS)
    assert not ok and any(l.startswith("FAILED: .claude/settings.json cannot be read") for l in lines)


def test_a_folder_that_does_not_exist_fails_without_crashing(tmp_path):
    ok, lines = G.verify(tmp_path / "nope", ANSWERS)
    assert not ok and "FAILED: boot file CLAUDE.md exists" in lines


def test_the_command_line_verify_prints_the_facts_and_the_verdict(fresh, tmp_path, capsys):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(ANSWERS), encoding="utf-8")
    assert G.main(["--verify", str(fresh), "--answers", str(answers)]) == 0
    out = capsys.readouterr().out
    assert out.strip().endswith("VERIFIED: the folder is complete") and "ok: boot file CLAUDE.md exists" in out
    (fresh / "CLAUDE.md").unlink()
    assert G.main(["--verify", str(fresh)]) == 1
    assert "NOT VERIFIED" in capsys.readouterr().out


def test_building_still_needs_both_answers_and_out(capsys):
    assert G.main(["--answers", "x.json"]) == 2
    assert "--answers and --out are required" in capsys.readouterr().err


def test_a_built_folder_always_has_the_autopilot_switched_off_even_from_a_kit_that_lost_its_template(tmp_path):
    kit = tmp_path / "kit"
    shutil.copytree(KIT, kit, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests", "logs"))
    shutil.rmtree(kit / "templates" / "logs", ignore_errors=True)                      # what an export that skipped every folder called logs would have produced
    assert not (kit / "templates" / "logs").exists()
    out = tmp_path / "built"
    report = G.generate(kit, ANSWERS, out)
    off = out / "logs" / "autopilot_off.txt"
    assert off.is_file() and "OFF" in off.read_text(encoding="utf-8") and "logs/autopilot_off.txt" in report["created"]


def test_the_kit_template_text_wins_over_the_plain_default_when_it_exists(tmp_path):
    out = tmp_path / "built"
    G.generate(KIT, ANSWERS, out)
    assert "Penny" in (out / "logs" / "autopilot_off.txt").read_text(encoding="utf-8")
