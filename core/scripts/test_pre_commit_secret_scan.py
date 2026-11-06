# -*- coding: utf-8 -*-
"""Tests for pre_commit_secret_scan.py: diff parsing, what counts as a secret, the hook text, install (never overwrites a foreign hook), the check and a real commit in a throwaway repo.
Every fake key is assembled from pieces at run time so no key-shaped text sits in this file."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pre_commit_secret_scan as pc  # noqa: E402

KEY = "sk-" + "Ab3dEf6hIj9kLm2NoPq5RsT8"
AWS = "AKIA" + "IOSFODNN7ABCDEFG"
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
RANDOM_VALUE = "Xk9fLm2QpR7sTvW4zYbN"
HAS_GIT = shutil.which("git") is not None
HAS_SH = shutil.which("sh") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git is not installed")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)       # the user's own git settings must not leak into the throwaway repos
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return root


def logs():
    return Path(os.environ["HARNESS_LOGS"])


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)


def make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    assert git(repo, "init", "-q").returncode == 0
    return repo


def stage(repo, name, text):
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    assert git(repo, "add", name).returncode == 0


DIFF = (
    "diff --git a/app.py b/app.py\n"
    "index 111..222 100644\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,0 +2,2 @@\n"
    "+first added\n"
    "+second added\n"
    "@@ -10 +12 @@\n"
    "-old\n"
    "+replaced\n"
    "diff --git a/gone.txt b/gone.txt\n"
    "deleted file mode 100644\n"
    "--- a/gone.txt\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n"
    "-bye\n"
    "diff --git a/dir/new file.md b/dir/new file.md\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/dir/new file.md\t\n"
    "@@ -0,0 +1 @@\n"
    "+++ not a header, just content\n"
)


# ---- diff parsing
def test_parse_added_lines_gives_file_new_line_number_and_text():
    got = list(pc.parse_added_lines(DIFF))
    assert got == [("app.py", 2, "first added"), ("app.py", 3, "second added"), ("app.py", 12, "replaced"), ("dir/new file.md", 1, "++ not a header, just content")]


def test_a_deleted_file_and_the_plus_plus_plus_header_are_never_scanned():
    assert not any(p == "gone.txt" for p, _n, _t in pc.parse_added_lines(DIFF))
    assert not any(t.startswith("++ b/") for _p, _n, t in pc.parse_added_lines(DIFF))


def test_parse_added_lines_of_nothing_or_junk_is_empty():
    assert list(pc.parse_added_lines("")) == [] and list(pc.parse_added_lines("hello\n+not in a file\n")) == []


# ---- what counts
@pytest.mark.parametrize("line,kind", [
    ("key = '%s'" % KEY, "known_key"),
    (AWS, "known_key"),
    (PEM, "private_key"),
    ('password = "%s"' % RANDOM_VALUE, "assignment"),
    ("API_TOKEN=%s" % ("Zq8wXe3rTy6uIo1pAs4dFg7hJk2l"), "env_line"),
])
def test_each_kind_of_secret_is_found(line, kind):
    assert kind in pc.scan_added_line(line)


@pytest.mark.parametrize("line", ["", "def f(): return 1", "password = get_password()", "token = 'short'", "name = 'value'", "see the docs at https://example.org/keys"])
def test_ordinary_lines_are_clean(line):
    assert pc.scan_added_line(line) == []


def test_a_placeholder_word_or_the_allow_marker_skips_the_line():
    assert pc.scan_added_line("api_key = '%s'  # example only" % KEY) == []
    assert pc.scan_added_line("password = 'changeme-%s'" % RANDOM_VALUE) == []
    assert pc.scan_added_line("%s  # secret-scan: allow" % KEY) == []
    assert pc.scan_added_line("%s  # SECRET-SCAN: ALLOW" % KEY) == []


def test_a_comment_line_skips_the_loose_patterns_but_not_a_real_key_format():
    assert pc.scan_added_line('# password = "%s"' % RANDOM_VALUE) == []
    assert pc.scan_added_line('// token: "%s"' % RANDOM_VALUE) == []
    assert "known_key" in pc.scan_added_line("# my key is %s" % KEY)


def test_a_very_long_line_is_cut_and_still_scanned_fast():
    line = "x " * 50000 + KEY
    assert pc.scan_added_line(line) == []        # the key is past the cut-off, the cost stays bounded
    assert "known_key" in pc.scan_added_line(KEY + " " + "y" * 50000)


def test_scan_diff_reports_place_and_kind_only_never_the_value():
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,2 @@\n+ok = 1\n+k = '%s'\n" % KEY
    found = pc.scan_diff(diff)
    assert found == [("a.py", 2, "known_key")]
    assert KEY not in json.dumps(found)


def test_scan_diff_without_a_detector_finds_nothing(monkeypatch):
    monkeypatch.setattr(pc, "_detector", lambda: None)
    assert pc.scan_diff("diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+%s\n" % KEY) == []


# ---- the hook text
def test_hook_script_quotes_awkward_paths_and_carries_the_marker():
    text = pc.hook_script(scanner="/odd dir/it's/pre_commit_secret_scan.py", python="/usr/bin/python3")
    assert text.startswith("#!/bin/sh\n") and pc.HOOK_MARKER in text
    assert "'/odd dir/it'\"'\"'s/pre_commit_secret_scan.py'" in text
    assert "--check-staged" in text and "exit $code" in text


def test_hook_script_uses_forward_slashes_even_for_windows_style_paths():
    text = pc.hook_script(scanner="C:\\tools\\pre_commit_secret_scan.py", python="C:\\Python\\python.exe")
    assert "\\" not in text.replace("\\n", "")


@pytest.mark.skipif(not HAS_SH, reason="sh is not installed")
def test_hook_script_is_valid_sh(tmp_path):
    f = tmp_path / "hook"
    f.write_text(pc.hook_script(), encoding="utf-8", newline="\n")
    assert subprocess.run(["sh", "-n", str(f)], capture_output=True, timeout=10).returncode == 0


# ---- install
@needs_git
def test_install_writes_the_hook_and_is_idempotent(tmp_path):
    repo = make_repo(tmp_path)
    code, msg = pc.install_hook(repo)
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert code == 0 and "Installed pre-commit hook" in msg and hook.exists() and pc.HOOK_MARKER in hook.read_text(encoding="utf-8")
    assert b"\r\n" not in hook.read_bytes()
    assert pc.install_hook(repo)[0] == 0
    assert not list((repo / ".git" / "hooks").glob("pre-commit.before*"))


@needs_git
def test_install_does_not_overwrite_a_different_hook_without_force(tmp_path):
    repo = make_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    code, msg = pc.install_hook(repo)
    assert code == 1 and "different pre-commit hook" in msg
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"


@needs_git
def test_install_force_keeps_the_old_hook_next_to_the_new_one(tmp_path):
    repo = make_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    code, _ = pc.install_hook(repo, force=True)
    assert code == 0 and pc.HOOK_MARKER in hook.read_text(encoding="utf-8")
    assert (repo / ".git" / "hooks" / "pre-commit.before-secret-scan").read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"


@needs_git
def test_install_dry_run_prints_the_plan_and_writes_nothing(tmp_path):
    repo = make_repo(tmp_path)
    code, msg = pc.install_hook(repo, dry_run=True)
    assert code == 0 and "dry run: would write" in msg and "--check-staged" in msg
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()


@needs_git
def test_install_in_a_folder_that_is_not_a_repo_fails_cleanly(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    code, msg = pc.install_hook(plain)
    assert code == 2 and "not a git repository" in msg


@needs_git
def test_install_honours_core_hookspath(tmp_path):
    repo = make_repo(tmp_path)
    shared = tmp_path / "shared-hooks"
    git(repo, "config", "core.hooksPath", str(shared))
    code, _ = pc.install_hook(repo)
    assert code == 0 and (shared / "pre-commit").exists() and not (repo / ".git" / "hooks" / "pre-commit").exists()


def test_install_without_git_reports_it(tmp_path, monkeypatch):
    def nogit(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(pc, "_git", nogit)
    code, msg = pc.install_hook(tmp_path)
    assert code == 2 and "git could not run" in msg


# ---- check_staged
@needs_git
def test_check_staged_blocks_a_secret_names_the_place_and_hides_the_value(tmp_path, capsys):
    repo = make_repo(tmp_path)
    stage(repo, "src/config.py", "a = 1\nkey = '%s'\n" % KEY)
    assert pc.check_staged(repo) == 1
    out = capsys.readouterr().out
    assert "COMMIT BLOCKED" in out and "src/config.py:2  known_key" in out and KEY not in out
    rows = [json.loads(l) for l in (logs() / "pre_commit_secret_scan.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["findings"] == [{"file": "src/config.py", "line": 2, "kind": "known_key"}] and KEY not in json.dumps(rows)


@needs_git
def test_check_staged_passes_a_clean_commit(tmp_path, capsys):
    repo = make_repo(tmp_path)
    stage(repo, "a.txt", "nothing secret here\n")
    assert pc.check_staged(repo) == 0 and capsys.readouterr().out == ""


@needs_git
def test_check_staged_ignores_unstaged_and_deleted_content(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "unstaged.txt").write_text(KEY, encoding="utf-8")        # not added, so not part of the commit
    stage(repo, "ok.txt", "fine\n")
    assert pc.check_staged(repo) == 0


@needs_git
def test_check_staged_handles_a_non_utf8_file_without_crashing(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "latin.txt").write_bytes(b"caf\xe9 \xff\xfe text\n")
    git(repo, "add", "latin.txt")
    assert pc.check_staged(repo) == 0


@needs_git
def test_the_kill_switch_lets_everything_through(tmp_path, capsys):
    repo = make_repo(tmp_path)
    stage(repo, "k.py", "k = '%s'\n" % KEY)
    (logs() / "pre_commit_secret_scan_off.txt").write_text("", encoding="utf-8")
    assert pc.check_staged(repo) == 0 and "off" in capsys.readouterr().out


def test_a_broken_git_lets_the_commit_through_with_a_warning(tmp_path, monkeypatch, capsys):
    def nogit(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(pc, "_git", nogit)
    assert pc.check_staged(tmp_path) == 0
    assert "git is not installed" in capsys.readouterr().err


def test_a_git_timeout_and_a_git_error_also_fail_open(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pc, "_git", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 1)))
    assert pc.check_staged(tmp_path) == 0 and "longer than" in capsys.readouterr().err
    monkeypatch.setattr(pc, "_git", lambda *a, **k: subprocess.CompletedProcess([], 128, b"", b"fatal: not a git repository"))
    assert pc.check_staged(tmp_path) == 0 and "git diff failed" in capsys.readouterr().err


def test_an_unloadable_scanner_fails_open(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pc, "_detector", lambda: None)
    assert pc.check_staged(tmp_path) == 0 and "could not be loaded" in capsys.readouterr().err


def test_an_oversize_diff_is_scanned_up_to_the_cap_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pc, "MAX_DIFF_BYTES", 40)
    big = b"diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+" + b"x" * 200 + b"\n"
    monkeypatch.setattr(pc, "_git", lambda *a, **k: subprocess.CompletedProcess([], 0, big, b""))
    assert pc.check_staged(tmp_path) == 0
    assert "only the first part" in capsys.readouterr().err


def test_no_creation_flags_are_passed_off_windows(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(pc.subprocess, "run", fake_run)
    monkeypatch.setattr(pc.sys, "platform", "linux")
    pc._git(".", "status")
    assert "creationflags" not in seen
    monkeypatch.setattr(pc.sys, "platform", "win32")
    pc._git(".", "status")
    assert "creationflags" in seen


# ---- main and a real commit
def test_main_without_options_prints_usage(capsys):
    assert pc.main([]) == 0 and "Usage" in capsys.readouterr().out


@needs_git
def test_main_install_dry_run_and_check(tmp_path, capsys):
    repo = make_repo(tmp_path)
    assert pc.main(["--install", "--dry-run", "--repo", str(repo)]) == 0
    assert "dry run" in capsys.readouterr().out
    stage(repo, "x.py", "k = '%s'\n" % KEY)
    assert pc.main(["--check-staged", "--repo", str(repo)]) == 1


@needs_git
@pytest.mark.skipif(not HAS_SH, reason="sh is not installed")
def test_a_real_commit_is_blocked_by_the_installed_hook_and_allowed_when_clean(tmp_path):
    repo = make_repo(tmp_path)
    assert pc.install_hook(repo)[0] == 0
    commit = ["-c", "user.name=tester", "-c", "user.email=tester@example.com", "commit", "-q", "-m", "x"]
    stage(repo, "bad.py", "k = '%s'\n" % KEY)
    r = git(repo, *commit)
    assert r.returncode != 0 and "COMMIT BLOCKED" in (r.stdout + r.stderr) and "bad.py:1" in (r.stdout + r.stderr)
    assert git(repo, "rev-parse", "--verify", "HEAD").returncode != 0         # nothing was committed
    git(repo, "rm", "-q", "--cached", "bad.py")
    stage(repo, "good.py", "k = 1\n")
    r = git(repo, *commit)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(repo, "rev-parse", "--verify", "HEAD").returncode == 0
