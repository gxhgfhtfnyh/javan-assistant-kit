# -*- coding: utf-8 -*-
"""
Tests for self_heal_check.py helpers.

Hard rules followed here:
- every module-level path is pointed at pytest's tmp_path (autouse fixture);
- subprocess is replaced by a recorder with canned return codes, and _notify
  (the module's best-effort owner-notification hook, which talks to the shared
  notify module) is replaced by a list append, so nothing runs or restarts and
  no real notify module needs to be importable for these tests;
- the module is imported only after reading its source confirmed that
  importing it runs no code beyond definitions and constants (see
  test_import_has_no_top_level_side_effects).
"""
import ast
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import self_heal_check as sh  # noqa: E402


class FakeRunner:
    """Stands in for the subprocess module. Records calls, runs nothing."""

    def __init__(self):
        self.calls = []
        self.returncode = 1
        self.on_run = None  # optional callable(args, cwd) to simulate side effects

    def run(self, args, cwd=None, capture_output=None, text=None):
        self.calls.append({"args": list(args), "cwd": cwd})
        if self.on_run is not None:
            self.on_run(args, cwd)
        return types.SimpleNamespace(returncode=self.returncode, stdout="", stderr="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "harness"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    notifications = []
    runner = FakeRunner()
    monkeypatch.setattr(sh, "CLAUDE_MD", root / "CLAUDE.md")
    monkeypatch.setattr(sh, "CLAUDE_MD_BACKUP", scripts / "_claude_md_backup.md")
    monkeypatch.setattr(sh, "SETTINGS_JSON", root / ".claude" / "settings.json")
    monkeypatch.setattr(sh, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(sh, "LOGS", root / "logs")
    monkeypatch.setattr(sh, "LOG_PATH", root / "logs" / "self_heal.jsonl")
    monkeypatch.setattr(sh, "_notify", lambda text: notifications.append(text))
    monkeypatch.setattr(sh, "subprocess", types.SimpleNamespace(run=runner.run))
    (root / "CLAUDE.md").write_bytes(b"# rules")  # present by default; CLAUDE.md tests delete it
    return types.SimpleNamespace(root=root, scripts=scripts, notifications=notifications, runner=runner)


def hooks_for(*commands):
    return {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": c} for c in commands]}]}}


def write_settings(data):
    sh.SETTINGS_JSON.parent.mkdir(parents=True, exist_ok=True)
    sh.SETTINGS_JSON.write_text(json.dumps(data), encoding="utf-8")


def make_restore_on_run(target):
    """Simulate a successful `git checkout HEAD -- <file>` by writing the file."""
    def on_run(args, cwd):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("restored", encoding="utf-8")
    return on_run


# ---------------------------------------------------------------- import safety

def test_import_has_no_top_level_side_effects():
    src = Path(sh.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign)):
            continue
        if isinstance(node, ast.If) and "__name__" in ast.dump(node.test):
            continue
        if isinstance(node, ast.Expr):
            if isinstance(node.value, ast.Constant):
                continue  # module docstring
            if ast.unparse(node.value).startswith("sys.path.insert("):
                continue
        pytest.fail(f"unexpected top-level statement at line {node.lineno}: {type(node).__name__}")


# --------------------------------------------------------------- default paths

def test_root_defaults_to_parent_of_scripts_dir():
    assert sh.ROOT == sh.SCRIPTS_DIR.parent


def test_logs_defaults_under_root():
    assert sh.LOGS == (sh.ROOT / "logs").resolve()


def test_settings_json_defaults_to_project_local_claude_dir():
    # Generalized from the live source's hardcoded Path.home() / ".claude" / "settings.json"
    # (a single-machine, user-global path) to this kit's own convention of a project-local
    # settings file, matching integrity_check.py and dead_script_check.py in this same folder.
    assert sh.SETTINGS_JSON == sh.ROOT / ".claude" / "settings.json"


def test_claude_md_backup_lives_in_scripts_dir():
    assert sh.CLAUDE_MD_BACKUP == sh.SCRIPTS_DIR / "_claude_md_backup.md"


# ------------------------------------------------------ _hook_paths_from_settings

def test_missing_settings_file_returns_empty(env):
    assert sh._hook_paths_from_settings() == []


def test_corrupt_settings_json_returns_empty(env):
    sh.SETTINGS_JSON.parent.mkdir(parents=True, exist_ok=True)
    sh.SETTINGS_JSON.write_text("{not json", encoding="utf-8")
    assert sh._hook_paths_from_settings() == []


def test_extracts_script_paths_from_nested_hooks(env):
    p1 = str(env.scripts / "guard.py")
    p2 = str(env.scripts / "notify.ps1")
    write_settings(hooks_for(f"python {p1}", f"powershell -File {p2} -Quiet"))
    assert sh._hook_paths_from_settings() == sorted([p1, p2])


def test_commands_without_script_paths_are_ignored(env):
    write_settings(hooks_for("echo hello", "npm test", "git status"))
    assert sh._hook_paths_from_settings() == []


def test_duplicate_paths_reported_once(env):
    p = str(env.scripts / "guard.py")
    write_settings(hooks_for(f"python {p}", f"python {p} --verbose"))
    assert sh._hook_paths_from_settings() == [p]


def test_quoted_path_stops_at_closing_quote(env):
    p = str(env.scripts / "guard.py")
    write_settings(hooks_for(f'python "{p}"'))
    assert sh._hook_paths_from_settings() == [p]


def test_non_string_command_values_are_ignored(env):
    write_settings({"command": 5, "other": {"command": None}, "list": [{"command": ["x.py"]}]})
    assert sh._hook_paths_from_settings() == []


def test_relative_script_paths_are_not_matched(env):
    write_settings(hooks_for("python scripts\\guard.py"))
    assert sh._hook_paths_from_settings() == []


def test_command_key_found_at_any_depth(env):
    p = str(env.scripts / "deep.bat")
    write_settings({"a": [{"b": {"c": {"command": f"call {p}"}}}]})
    assert sh._hook_paths_from_settings() == [p]


# --------------------------------------------------------- _restore_from_scripts_git

def test_path_outside_scripts_dir_is_refused_without_git(env):
    outside = env.root / "elsewhere" / "tool.py"
    assert sh._restore_from_scripts_git(outside) is False
    assert env.runner.calls == []


def test_successful_checkout_restores_and_uses_scripts_cwd(env):
    target = env.scripts / "hook.py"
    env.runner.returncode = 0
    env.runner.on_run = make_restore_on_run(target)
    assert sh._restore_from_scripts_git(target) is True
    call = env.runner.calls[0]
    assert call["args"] == ["git", "checkout", "HEAD", "--", "hook.py"]
    assert call["cwd"] == str(env.scripts)


def test_subfolder_script_uses_relative_pathspec(env):
    target = env.scripts / "sub" / "a.py"
    env.runner.returncode = 0
    env.runner.on_run = make_restore_on_run(target)
    assert sh._restore_from_scripts_git(target) is True
    assert env.runner.calls[0]["args"][-1] == str(Path("sub") / "a.py")


def test_nonzero_git_exit_is_a_failure(env):
    target = env.scripts / "hook.py"
    env.runner.returncode = 1
    assert sh._restore_from_scripts_git(target) is False


def test_git_zero_exit_without_file_on_disk_is_a_failure(env):
    target = env.scripts / "hook.py"
    env.runner.returncode = 0  # git says ok but nothing was written
    assert sh._restore_from_scripts_git(target) is False


# ------------------------------------------------------------------------- check()

def test_everything_present_reports_nothing(env):
    write_settings({})
    result = sh.check()
    assert result["restored"] == []
    assert result["still_missing"] == []
    assert env.notifications == []
    assert env.runner.calls == []


def test_missing_claude_md_restored_from_backup(env):
    sh.CLAUDE_MD.unlink()
    sh.CLAUDE_MD_BACKUP.parent.mkdir(parents=True, exist_ok=True)
    sh.CLAUDE_MD_BACKUP.write_bytes(b"# backed up rules\n")
    write_settings({})
    result = sh.check()
    assert result["restored"] == [str(sh.CLAUDE_MD)]
    assert sh.CLAUDE_MD.read_bytes() == b"# backed up rules\n"
    assert "restored" in env.notifications[0]


def test_missing_claude_md_without_backup_is_flagged(env):
    sh.CLAUDE_MD.unlink()
    write_settings({})
    result = sh.check()
    assert result["restored"] == []
    assert result["still_missing"] == [str(sh.CLAUDE_MD)]
    assert str(sh.CLAUDE_MD) in env.notifications[0]


def test_missing_settings_is_flagged_not_created(env):
    result = sh.check()
    assert result["still_missing"] == [str(sh.SETTINGS_JSON) + " (no backup source available)"]
    assert not sh.SETTINGS_JSON.exists()


def test_missing_hook_restored_through_git(env):
    hook = env.scripts / "guard.py"
    write_settings(hooks_for(f"python {hook}"))
    env.runner.returncode = 0
    env.runner.on_run = make_restore_on_run(hook)
    result = sh.check()
    assert result["restored"] == [str(hook)]
    assert result["still_missing"] == []


def test_missing_hook_outside_scripts_is_not_restored(env):
    outside = env.root / "tools" / "x.py"
    write_settings(hooks_for(f"python {outside}"))
    result = sh.check()
    assert result["still_missing"] == [str(outside) + " (outside the scripts git repo, no backup source)"]
    assert env.runner.calls == []


def test_missing_hook_with_git_failure_is_flagged(env):
    hook = env.scripts / "guard.py"
    write_settings(hooks_for(f"python {hook}"))
    env.runner.returncode = 1
    result = sh.check()
    assert result["still_missing"] == [str(hook) + " (git restore failed or not tracked)"]
    assert str(hook) in env.notifications[0]


def test_present_hook_is_not_touched(env):
    hook = env.scripts / "guard.py"
    hook.write_text("print(1)", encoding="utf-8")
    write_settings(hooks_for(f"python {hook}"))
    result = sh.check()
    assert result["restored"] == [] and result["still_missing"] == []
    assert env.runner.calls == []


def test_log_file_gets_one_json_line_per_run(env):
    write_settings({})
    sh.check()
    sh.check()
    lines = sh.LOG_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        record = json.loads(line)
        assert set(record) == {"timestamp", "restored", "still_missing"}


def test_notification_text_is_truncated_to_200_chars(env):
    long_name = "a" * 110 + ".py"
    outside = env.root / "tools" / long_name
    write_settings(hooks_for(f"python {outside}"))
    sh.check()
    assert env.notifications, "expected a notification for the missing file"
    assert len(env.notifications[-1]) <= 200


# ------------------------------------------------- known or likely module bugs

def test_script_path_with_spaces_is_found(env):
    p = env.scripts / "my hooks" / "guard.py"
    write_settings(hooks_for(f'python "{p}"'))
    assert sh._hook_paths_from_settings() == [str(p)]


def test_forward_slash_hook_path_is_found(env):
    p = env.scripts / "guard.py"
    write_settings(hooks_for(f"python {p.as_posix()}"))
    assert sh._hook_paths_from_settings() == [str(p)]


def test_settings_with_utf8_bom_is_parsed(env):
    p = env.scripts / "guard.py"
    sh.SETTINGS_JSON.parent.mkdir(parents=True, exist_ok=True)
    sh.SETTINGS_JSON.write_bytes(b"\xef\xbb\xbf" + json.dumps(hooks_for(f"python {p}")).encode("utf-8"))
    assert sh._hook_paths_from_settings() == [str(p)]


# --------------------------------------------------------------------- _notify

def test_notify_falls_back_to_alerts_txt_when_notify_module_missing(tmp_path, monkeypatch):
    """Exercises the real _notify (not the fixture's stub): with no importable `notify` module
    on sys.path, it must fall back to appending a line to logs/alerts.txt itself, never raise."""
    root = tmp_path / "harness2"
    logs = root / "logs"
    monkeypatch.setattr(sh, "LOGS", logs)
    monkeypatch.setattr(sh, "SCRIPTS_DIR", root / "scripts")
    monkeypatch.setitem(sys.modules, "notify", None)  # import notify -> ImportError
    sh._notify("hello world")
    text = (logs / "alerts.txt").read_text(encoding="utf-8")
    assert "self_heal_check" in text
    assert "hello world" in text
