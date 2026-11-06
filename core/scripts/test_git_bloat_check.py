# -*- coding: utf-8 -*-
"""Tests for git_bloat_check. git is faked everywhere except one test that makes a tiny throwaway repository inside tmp_path (skipped when git is not installed)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import git_bloat_check as gb  # noqa: E402

MB = 1024 * 1024


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def fake_repo(path):
    (path / ".git").mkdir(parents=True)
    return path


class FakeGit:
    """Stands in for git_run: answers rev-list and cat-file from a table of (hash, type, size, path)."""
    def __init__(self, objects, rev_code=0, check_code=0):
        self.objects, self.rev_code, self.check_code, self.calls = objects, rev_code, check_code, []

    def __call__(self, args, input_text=None, timeout=60):
        self.calls.append((list(args), input_text, timeout))
        if "rev-list" in args:
            return self.rev_code, "".join("%s %s\n" % (h, p) if p else "%s\n" % h for h, _, _, p in self.objects)
        return self.check_code, "".join("%s %s %d\n" % (h, t, s) for h, t, s, _ in self.objects)


OBJECTS = [("c1", "commit", 200, ""), ("t1", "tree", 100, ""), ("b1", "blob", 9 * MB, "assets/video file.mp4"), ("b2", "blob", 6 * MB, "dump.sql"),
           ("b3", "blob", 1 * MB, "small.png")]


# ---------- pure parsing ----------
def test_parse_rev_list_keeps_paths_with_spaces_and_bare_hashes():
    out = gb.parse_rev_list("aaa\nbbb dir/my file.txt\n\n  \nccc x.bin\r\n")
    assert out == {"aaa": "", "bbb": "dir/my file.txt", "ccc": "x.bin"}


def test_parse_rev_list_of_nothing_is_empty():
    assert gb.parse_rev_list("") == {} and gb.parse_rev_list(None) == {}


def test_parse_batch_check_keeps_only_blobs_over_the_threshold_largest_first():
    text = "b2 blob %d\nb1 blob %d\nt1 tree %d\nb3 blob %d\n" % (6 * MB, 9 * MB, 99 * MB, MB)
    out = gb.parse_batch_check(text, {"b1": "big.mp4", "b2": "dump.sql"}, 5 * MB)
    assert [o["path"] for o in out] == ["big.mp4", "dump.sql"] and out[0]["size_mb"] == 9.0


def test_parse_batch_check_ignores_bad_lines_and_names_unknown_paths():
    text = "x blob notanumber\nonly two\nz blob %d\nmissing blob 3 extra\n" % (7 * MB)
    out = gb.parse_batch_check(text, {}, 5 * MB)
    assert out == [{"path": "(unknown)", "size_mb": 7.0, "hash": "z"}]


def test_an_object_exactly_at_the_threshold_is_not_reported():
    assert gb.parse_batch_check("a blob %d\n" % (5 * MB), {"a": "x"}, 5 * MB) == []


# ---------- find_large_objects ----------
def test_find_large_objects_reports_blobs_and_asks_git_the_right_questions():
    fake = FakeGit(OBJECTS)
    out = gb.find_large_objects("/some/repo", 5, run=fake, timeout=7)
    assert [o["path"] for o in out] == ["assets/video file.mp4", "dump.sql"]
    (a1, in1, t1), (a2, in2, t2) = fake.calls
    assert a1 == ["-C", "/some/repo", "rev-list", "--objects", "--all"] and in1 is None and t1 == 7
    assert a2[:3] == ["-C", "/some/repo", "cat-file"] and a2[3].startswith("--batch-check=") and in2.split() == ["c1", "t1", "b1", "b2", "b3"]


def test_an_empty_repository_gives_no_findings_and_no_second_git_call():
    fake = FakeGit([])
    assert gb.find_large_objects("r", 5, run=fake) == [] and len(fake.calls) == 1


def test_a_failing_rev_list_is_an_error_not_an_empty_result():
    with pytest.raises(gb.GitError):
        gb.find_large_objects("r", 5, run=FakeGit(OBJECTS, rev_code=128))


def test_a_failing_cat_file_is_an_error():
    with pytest.raises(gb.GitError):
        gb.find_large_objects("r", 5, run=FakeGit(OBJECTS, check_code=1))


def test_git_run_turns_a_missing_git_and_a_timeout_into_git_errors(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(gb.GitError, match="not installed"):
        gb.git_run(["status"])

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("git", 1)
    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(gb.GitError, match="longer than"):
        gb.git_run(["status"], timeout=1)


def test_git_run_passes_no_creation_flags_off_windows_and_never_prompts(monkeypatch):
    seen = {}

    class R:
        returncode, stdout = 0, "ok"

    def fake_run(argv, **kw):
        seen.update(kw, argv=argv)
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)       # what a Mac or Linux Python looks like
    assert gb.git_run(["status"]) == (0, "ok")
    assert seen["creationflags"] == 0 and seen["argv"] == ["git", "status"] and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


# ---------- which repositories ----------
def test_default_repos_are_the_vault_and_the_folder_only_when_they_are_repos(home):
    assert gb.default_repos() == []
    fake_repo(home / "vault")
    assert gb.default_repos() == [(home / "vault").resolve()]
    fake_repo(home)
    assert gb.default_repos() == [(home / "vault").resolve(), home.resolve()]


# ---------- the command line ----------
def test_main_without_repos_says_so_and_exits_zero(home, capsys):
    assert gb.main([]) == 0
    assert "no git repositories to check" in capsys.readouterr().out


def test_main_prints_findings_for_a_repo(home, monkeypatch, capsys):
    repo = fake_repo(home / "repo")
    monkeypatch.setattr(gb, "git_run", FakeGit(OBJECTS))
    assert gb.main([str(repo), "--threshold-mb", "5"]) == 0
    out = capsys.readouterr().out
    assert "9.0 MB  assets/video file.mp4" in out and "6.0 MB  dump.sql" in out and "small.png" not in out


def test_main_says_when_nothing_is_over_the_threshold(home, monkeypatch, capsys):
    repo = fake_repo(home / "repo")
    monkeypatch.setattr(gb, "git_run", FakeGit(OBJECTS))
    gb.main([str(repo), "--threshold-mb", "50"])
    assert "no objects over the threshold" in capsys.readouterr().out


def test_main_skips_a_folder_that_is_not_a_repo_and_a_missing_path(home, capsys):
    (home / "plain").mkdir()
    assert gb.main([str(home / "plain"), str(home / "nope")]) == 0
    assert capsys.readouterr().out.count("not a git repository, skipped") == 2


def test_main_reports_a_git_failure_and_goes_on_to_the_next_repo(home, monkeypatch, capsys):
    a, b = fake_repo(home / "a"), fake_repo(home / "b")
    calls = []

    def run(args, input_text=None, timeout=60):
        calls.append(args[1])
        return (128, "") if args[1].endswith("a") else FakeGit(OBJECTS)(args, input_text, timeout)
    monkeypatch.setattr(gb, "git_run", run)
    assert gb.main([str(a), str(b)]) == 0
    out = capsys.readouterr().out
    assert "could not check" in out and "dump.sql" in out


def test_main_stops_quietly_when_git_is_not_installed(home, monkeypatch, capsys):
    repo = fake_repo(home / "repo")

    def run(*a, **k):
        raise gb.GitError("git is not installed")
    monkeypatch.setattr(gb, "git_run", run)
    assert gb.main([str(repo)]) == 0
    assert "git is not installed" in capsys.readouterr().out


def test_main_rejects_a_zero_threshold(home):
    with pytest.raises(SystemExit):
        gb.main(["--threshold-mb", "0"])


def test_job_mode_honours_the_kill_switch(home, monkeypatch, capsys):
    repo = fake_repo(home / "repo")
    (home / "logs").mkdir()
    (home / "logs" / "git_bloat_check_off.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(gb, "git_run", FakeGit(OBJECTS))
    assert gb.main([str(repo), "--job"]) == 0
    assert "git_bloat_check: off" in capsys.readouterr().out and not (home / "logs" / "alerts.txt").exists()


def test_job_mode_raises_one_ask_first_item_through_notify_and_exits_zero(home, monkeypatch):
    repo = fake_repo(home / "repo")
    monkeypatch.setattr(gb, "git_run", FakeGit(OBJECTS))
    assert gb.main([str(repo), "--job"]) == 0
    alerts = (home / "logs" / "alerts.txt").read_text(encoding="utf-8")
    assert "git_bloat_check:" in alerts and "2 large file(s)" in alerts and "assets/video file.mp4" in alerts
    rows = [json.loads(l) for l in (home / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["kind"] == "git_bloat" and rows[0]["tier"] == 2


def test_a_job_run_with_nothing_found_raises_nothing(home, monkeypatch):
    repo = fake_repo(home / "repo")
    monkeypatch.setattr(gb, "git_run", FakeGit([OBJECTS[0]]))
    assert gb.main([str(repo), "--job"]) == 0
    assert not (home / "logs" / "alerts.txt").exists()


def test_the_summary_line_names_the_repo_the_biggest_file_and_the_owner():
    line = gb.summary_line("vault", [{"path": "a.zip", "size_mb": 12.34, "hash": "h"}, {"path": "b", "size_mb": 6, "hash": "g"}])
    assert "2 large file(s)" in line and "vault" in line and "a.zip" in line and "12.3 MB" in line and "ask the owner" in line


# ---------- one real run against a throwaway repository ----------
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_real_repository_with_a_deleted_big_file_is_caught(tmp_path):
    repo = tmp_path / "real"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def git(*args):
        subprocess.run(["git", "-C", str(repo)] + list(args), env=env, check=True, capture_output=True, timeout=30)
    git("init", "-q")
    (repo / "big file.bin").write_bytes(os.urandom(2 * MB))
    git("add", ".")
    git("commit", "-q", "-m", "add")
    (repo / "big file.bin").unlink()
    git("add", "-A")
    git("commit", "-q", "-m", "remove")
    found = gb.find_large_objects(repo, 1)
    assert [o["path"] for o in found] == ["big file.bin"] and 1.9 < found[0]["size_mb"] < 2.1
    assert gb.find_large_objects(repo, 5) == []
