# -*- coding: utf-8 -*-
"""Tests for automation_miner.py: command keys, call pairing, ranking, finding the transcripts, the report and the saved note."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import automation_miner as am  # noqa: E402


# ---------------------------------------------------------------- command keys

@pytest.mark.parametrize("cmd,key", [
    ("cd /work/game && python run_test.py shots", "python run_test.py shots"),
    ("python /srv/me/tools/op_note.py a_1 \"text\" --kind step", "python op_note.py"),
    ("python send_message.py --stdin --reply-to 123 <<'EOF'\nhello\nEOF", "python send_message.py"),
    ("python C:\\proj\\scripts\\build.py", "python build.py"),
    ("python -m pytest -q tests", "python -m pytest"),
    ("grep -n foo bar.py | head -3", "grep -n"),
    ("sed -n 1,20p file.md", "sed -n"),
    ("ls /srv/app/scripts", "ls scripts"),
    ("sleep 5 && cat x.txt", "cat x.txt"),
    ("git status", "git status"),
    ("", "(empty)"),
    (None, "(empty)"),
    ("cd /tmp", "(empty)"),
])
def test_command_key(cmd, key):
    assert am.command_key(cmd) == key


def test_numbers_do_not_split_keys():
    assert am.command_key("tail -5 log.txt") == am.command_key("tail -9 log.txt")


def test_environment_assignments_and_secrets_never_reach_the_key():
    assert am.command_key("API_TOKEN=abc123secret python job.py run") == "python job.py run"
    assert "secret" not in am.command_key("curl --header=secretvalue https://example.test/x?token=hunter2")
    assert "hunter2" not in am.command_key("curl https://example.test/x?token=hunter2")
    assert "xyz" not in am.command_key('python tool.py "password xyz"')


def test_keys_are_capped_in_length():
    assert len(am.command_key("python " + "a" * 300 + ".py")) <= 80


# ---------------------------------------------------------------- pairing calls with results

def _use(i, name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": str(i), "name": name, "input": inp}]}}


def _res(i, text, err=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": str(i), "content": text, "is_error": err}]}}


def test_extract_pairs_results_with_calls():
    rows = [_use(1, "Bash", command="python a.py"), _res(1, "x" * 50), _use(2, "Read", file_path="/f.md"), _res(2, [{"type": "text", "text": "abc"}])]
    calls = am.extract_calls(rows)
    assert [c["key"] for c in calls] == ["python a.py", "Read"]
    assert [c["out_chars"] for c in calls] == [50, 3]
    assert calls[1]["file"] == "/f.md" and calls[0]["file"] is None


def test_extract_handles_powershell_error_flag_and_unknown_results():
    calls = am.extract_calls([_use(1, "PowerShell", command="Get-Date"), _res(1, "boom", err=True), _res(99, "orphan result")])
    assert calls[0]["key"] == "Get-Date" and calls[0]["is_error"] is True and len(calls) == 1


def test_extract_ignores_junk_rows():
    assert am.extract_calls([None, "x", {"message": {"content": "str"}}, {"type": "assistant"}, {"message": "text"}]) == []


def test_extract_survives_odd_field_types():
    rows = [_use(1, "Bash", command=["not", "a", "string"]), _use(2, "Read", file_path=5), {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "3", "name": "Bash", "input": "oops"}]}},
            _res(1, {"weird": True}), _res(2, [{"type": "text", "text": None}, "plain"])]
    calls = am.extract_calls(rows)
    assert [c["key"] for c in calls] == ["(empty)", "Read", "(empty)"] and calls[1]["file"] is None and calls[0]["out_chars"] == 0


def test_a_call_logged_twice_is_counted_once():
    assert len(am.extract_calls([_use(1, "Bash", command="ls"), _use(1, "Bash", command="ls")])) == 1


# ---------------------------------------------------------------- ranking

def test_rank_repeated_reread_retry_huge():
    calls = []
    for i in range(3):
        calls.append({"id": i, "name": "Bash", "key": "python a.py", "out_chars": 10, "is_error": False, "file": None})
    for i in range(3):
        calls.append({"id": 10 + i, "name": "Read", "key": "Read", "out_chars": 7000, "is_error": False, "file": "/same.md"})
    calls.append({"id": 20, "name": "Bash", "key": "python b.py", "out_chars": 0, "is_error": True, "file": None})
    calls.append({"id": 21, "name": "Bash", "key": "python b.py", "out_chars": 0, "is_error": False, "file": None})
    r = am.rank(calls)
    assert r["repeated"]["python a.py"] == (3, 30)
    assert "python b.py" not in r["repeated"]          # only 2 calls
    assert r["reread"] == {"/same.md": 3}
    assert r["retries"] == {"python b.py": 1}
    assert r["huge"] == {"Read": 3}


def test_error_without_retry_is_not_a_retry_loop():
    calls = [{"id": 1, "name": "Bash", "key": "a", "out_chars": 0, "is_error": True, "file": None},
             {"id": 2, "name": "Bash", "key": "b", "out_chars": 0, "is_error": False, "file": None}]
    assert am.rank(calls)["retries"] == {}


def test_retry_window_is_three_calls():
    far = [{"id": 1, "name": "Bash", "key": "a", "out_chars": 0, "is_error": True, "file": None}] + \
          [{"id": 5 + i, "name": "Bash", "key": "x%d" % i, "out_chars": 0, "is_error": False, "file": None} for i in range(3)] + \
          [{"id": 9, "name": "Bash", "key": "a", "out_chars": 0, "is_error": False, "file": None}]
    assert am.rank(far)["retries"] == {}                # the repeat is 4 calls later, outside the window


def test_file_read_twice_is_not_flagged_but_three_times_is():
    def rd(n, f):
        return [{"id": i, "name": "Read", "key": "Read", "out_chars": 1, "is_error": False, "file": f} for i in range(n)]
    assert am.rank(rd(2, "/twice.md"))["reread"] == {}
    assert am.rank(rd(3, "/thrice.md"))["reread"] == {"/thrice.md": 3}


def test_output_exactly_at_the_limit_is_not_huge():
    one = [{"id": 1, "name": "Bash", "key": "k", "out_chars": am.BIG, "is_error": False, "file": None}]
    assert am.rank(one)["huge"] == {}
    one[0]["out_chars"] = am.BIG + 1
    assert am.rank(one)["huge"] == {"k": 1}


def test_rank_of_nothing_is_empty():
    assert am.rank([]) == {"repeated": {}, "reread": {}, "retries": {}, "huge": {}}


# ---------------------------------------------------------------- finding the transcripts

def make_log(folder, name, rows, mtime=None):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "claude-config"
    (cfg / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(am, "ROOT", tmp_path / "assistant")
    monkeypatch.setattr(am, "VAULT", tmp_path / "vault")
    work = tmp_path / "my project"                    # a folder name with a space, as Claude Code turns into a dash
    work.mkdir()
    monkeypatch.chdir(work)
    return {"projects": cfg / "projects", "work": work, "vault": tmp_path / "vault", "tmp": tmp_path}


def test_project_slug_turns_everything_but_letters_and_digits_into_dashes():
    assert am.project_slug("/srv/me/my app") == "-srv-me-my-app"
    assert am.project_slug("D:\\work\\proj") == "D--work-proj"


def test_projects_dir_follows_claude_config_dir(env, monkeypatch):
    assert am.projects_dir() == env["projects"]
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert am.projects_dir() == Path.home() / ".claude" / "projects"


def test_finds_the_transcripts_of_the_current_folder(env):
    folder = env["projects"] / am.project_slug(env["work"].resolve())
    make_log(folder, "s.jsonl", [])
    assert am.find_project_dir() == folder


def test_finds_the_transcripts_of_a_parent_folder(env):
    sub = env["work"] / "deep" / "er"
    sub.mkdir(parents=True)
    folder = env["projects"] / am.project_slug(env["work"].resolve())
    make_log(folder, "s.jsonl", [])
    assert am.find_project_dir(sub) == folder


def test_falls_back_to_the_assistant_folder(env):
    (env["tmp"] / "assistant").mkdir()
    folder = env["projects"] / am.project_slug((env["tmp"] / "assistant").resolve())
    make_log(folder, "s.jsonl", [])
    os.chdir(env["tmp"])                                            # a folder that has no transcripts of its own
    assert am.find_project_dir() == folder


def test_a_folder_without_transcripts_is_not_a_match(env):
    (env["projects"] / am.project_slug(env["work"].resolve())).mkdir()
    assert am.find_project_dir() is None


def test_resolve_dirs_accepts_a_transcript_folder_directly(env):
    folder = env["tmp"] / "plain-folder"
    make_log(folder, "a.jsonl", [])
    assert am.resolve_dirs(str(folder)) == [folder]


def test_resolve_dirs_for_a_named_project_and_for_all(env):
    other = env["tmp"] / "other-proj"
    other.mkdir()
    make_log(env["projects"] / am.project_slug(other.resolve()), "a.jsonl", [])
    make_log(env["projects"] / "second-project", "b.jsonl", [])
    (env["projects"] / "empty-project").mkdir()
    assert am.resolve_dirs(str(other)) == [env["projects"] / am.project_slug(other.resolve())]
    assert am.resolve_dirs(all_projects=True) == sorted([env["projects"] / am.project_slug(other.resolve()), env["projects"] / "second-project"])
    assert am.resolve_dirs(str(env["tmp"] / "nowhere")) == []


def test_list_transcripts_newest_first_top_level_only(env):
    d = env["projects"] / "p"
    now = time.time()
    make_log(d, "old.jsonl", [], now - 300)
    make_log(d, "new.jsonl", [], now - 10)
    make_log(d / "sess" / "subagents", "agent-x.jsonl", [], now)
    (d / "notes.txt").write_text("x")
    got = am.list_transcripts([d], 5)
    assert [os.path.basename(p) for p in got] == ["new.jsonl", "old.jsonl"]
    assert [os.path.basename(p) for p in am.list_transcripts([d], 1)] == ["new.jsonl"]
    assert am.list_transcripts([d / "missing"], 3) == []


# ---------------------------------------------------------------- reading and the report

def test_load_calls_streams_skips_junk_and_pairs_per_file(env):
    d = env["projects"] / "p"
    a = make_log(d, "a.jsonl", [_use(1, "Bash", command="python a.py"), _res(1, "y" * 20), {"type": "system", "text": "no tools here"}])
    with a.open("a", encoding="utf-8") as f:
        f.write("not json but mentions \"tool_use\"\n")
    b = make_log(d, "b.jsonl", [_use(1, "Bash", command="python b.py"), _res(1, "z" * 5)])        # same id as in a.jsonl: separate file, separate pairing
    calls, n = am.load_calls([str(a), str(b)])
    assert n == 2 and [(c["key"], c["out_chars"]) for c in calls] == [("python a.py", 20), ("python b.py", 5)]


def test_load_calls_skips_an_over_long_line_and_a_missing_file(env, monkeypatch):
    monkeypatch.setattr(am, "MAX_LINE", 1500)
    d = env["projects"] / "p"
    p = make_log(d, "a.jsonl", [_use(1, "Bash", command="ls"), _use(2, "Bash", command="echo " + "x" * 4000), _use(3, "Bash", command="pwd")])
    calls, _ = am.load_calls([str(p), str(d / "gone.jsonl")])
    assert [c["id"] for c in calls] == ["1", "3"]


def test_report_lists_sections_and_never_prints_tool_output():
    calls = [{"id": i, "name": "Bash", "key": "python a.py", "out_chars": 9000, "is_error": False, "file": None} for i in range(3)]
    calls += [{"id": 10 + i, "name": "Read", "key": "Read", "out_chars": 5, "is_error": False, "file": "/some/dir/Plan.md"} for i in range(3)]
    text = am.report(calls, 2, now=0)
    assert "## Repeated commands" in text and "`python a.py`: 3 calls, 27000 chars" in text
    assert "- Plan.md: 3 reads" in text and "/some/dir" not in text
    assert "6 tool calls in the 2 newest session transcripts" in text and "huge output (over 6000 chars)" in text
    assert "`python a.py`: 3 times" in text


def test_report_of_nothing_still_has_every_heading():
    text = am.report([], 0)
    assert text.count("## ") == 4 and "0 tool calls in the 0 newest" in text


# ---------------------------------------------------------------- the saved note

def test_note_has_front_matter_and_marker():
    t = am.note_text("body", now=0)
    assert t.startswith("---\n") and am.MARKER in t.split("---")[1] and t.rstrip().endswith("body")


def test_write_note_creates_folders_and_replaces_its_own_file(tmp_path):
    p = tmp_path / "vault" / "05 - Resources" / "r.md"
    am.write_note("one", p)
    am.write_note("two", p)
    assert "two" in p.read_text(encoding="utf-8") and "one" not in p.read_text(encoding="utf-8")
    assert [x.name for x in p.parent.iterdir()] == ["r.md"]                        # no temp file left behind


def test_write_note_refuses_to_replace_someone_elses_file(tmp_path):
    p = tmp_path / "mine.md"
    p.write_text("my own notes\n", encoding="utf-8")
    with pytest.raises(am.NotOurs):
        am.write_note("x", p)
    assert p.read_text(encoding="utf-8") == "my own notes\n"
    am.write_note("x", p, force=True)
    assert am.MARKER in p.read_text(encoding="utf-8")


def test_write_note_to_a_folder_path_fails_cleanly(tmp_path):
    with pytest.raises(OSError):
        am.write_note("x", tmp_path)


# ---------------------------------------------------------------- the command

def fill(env, name="s.jsonl"):
    folder = env["projects"] / am.project_slug(env["work"].resolve())
    rows = []
    for i in range(4):
        rows += [_use(i, "Bash", command="python collect.py go"), _res(i, "q" * 7000)]
    rows += [_use(50, "Read", file_path="/a/b/Spec.md"), _res(50, "s"), _use(51, "Read", file_path="/a/b/Spec.md"), _res(51, "s"),
             _use(52, "Read", file_path="/a/b/Spec.md"), _res(52, "s")]
    return make_log(folder, name, rows)


def test_main_prints_the_report(env, capsys):
    fill(env)
    assert am.main([]) == 0
    out = capsys.readouterr().out
    assert "`python collect.py go`: 4 calls" in out and "Spec.md: 3 reads" in out and "huge output" in out
    assert "qqqq" not in out


def test_main_with_no_transcripts_says_where_it_looked(env, capsys):
    assert am.main([]) == 1
    out = capsys.readouterr().out
    assert "no Claude Code transcripts found" in out and str(env["projects"]) in out


def test_main_write_saves_into_the_vault_resources_folder(env, capsys):
    fill(env)
    assert am.main(["--write"]) == 0
    note = env["vault"] / "05 - Resources" / "Automation Miner Report.md"
    assert note.exists() and "Spec.md" in note.read_text(encoding="utf-8") and "wrote" in capsys.readouterr().out


def test_main_out_chooses_the_file_and_force_is_needed_for_foreign_files(env, capsys):
    fill(env)
    target = env["tmp"] / "elsewhere" / "report.md"
    assert am.main(["--out", str(target)]) == 0 and target.exists()
    other = env["tmp"] / "notes.md"
    other.write_text("keep me", encoding="utf-8")
    assert am.main(["--out", str(other)]) == 1
    assert other.read_text(encoding="utf-8") == "keep me" and "--force" in capsys.readouterr().out
    assert am.main(["--out", str(other), "--force"]) == 0 and am.MARKER in other.read_text(encoding="utf-8")


def test_main_reports_a_write_failure_instead_of_crashing(env, monkeypatch, capsys):
    fill(env)

    def deny(src, dst):
        raise PermissionError("no")
    monkeypatch.setattr(am.os, "replace", deny)
    assert am.main(["--write"]) == 1
    assert "not written" in capsys.readouterr().out
    assert not list((env["vault"] / "05 - Resources").glob("*.tmp*"))


def test_main_without_write_touches_no_files(env):
    fill(env)
    before = sorted(p.name for p in env["tmp"].rglob("*"))
    assert am.main([]) == 0
    assert sorted(p.name for p in env["tmp"].rglob("*")) == before


def test_main_files_limit_and_all_projects(env, capsys):
    fill(env, "new.jsonl")
    make_log(env["projects"] / "another", "z.jsonl", [_use(1, "Bash", command="python other.py"), _res(1, "ok")], time.time() - 5000)
    assert am.main(["--files", "1", "--all-projects"]) == 0
    out = capsys.readouterr().out
    assert "in the 1 newest" in out and "python collect.py go" in out and "python other.py" not in out
    assert am.main(["--all-projects", "--files", "5"]) == 0
    assert "in the 2 newest" in capsys.readouterr().out


def test_main_project_option(env, capsys):
    other = env["tmp"] / "picked"
    other.mkdir()
    make_log(env["projects"] / am.project_slug(other.resolve()), "a.jsonl", [_use(1, "Bash", command="python picked.py"), _res(1, "x")])
    assert am.main(["--project", str(other)]) == 0
    assert "tool calls in the 1 newest" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--files", "0"], ["--files", "abc"], ["--files", "100000"], ["--nope"]])
def test_main_bad_arguments_exit_2(env, argv):
    assert am.main(argv) == 2


# ---------------------------------------------------------------- as a real process

def test_the_script_runs_as_a_process_with_its_own_environment(tmp_path):
    cfg = tmp_path / "cfg"
    work = tmp_path / "w"
    work.mkdir()
    make_log(cfg / "projects" / am.project_slug(work.resolve()), "a.jsonl", [_use(1, "Bash", command="python x.py"), _res(1, "ok")])
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"),
               CLAUDE_CONFIG_DIR=str(cfg), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-B", str(HERE / "automation_miner.py"), "--write"], cwd=str(work), env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / "vault" / "05 - Resources" / "Automation Miner Report.md").exists()
    assert "1 tool calls in the 1 newest" in r.stdout


def test_the_source_has_no_fixed_project_or_vault_path():
    src = (HERE / "automation_miner.py").read_text(encoding="utf-8")
    assert not re.search(r"\b[A-Za-z]:[\\/]", src) and "claude\\projects" not in src
