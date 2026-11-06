# -*- coding: utf-8 -*-
"""Tests for token_audit.py: trigger kinds, the triggers file, attributing usage, the report, finding the newest transcript and the command."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import token_audit as ta  # noqa: E402


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def results(*pairs):
    """A user row that only carries tool results: pairs of (content, ...)"""
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": c} for c in pairs]}}


def asst(mid, usage=(1, 2, 3, 4), tools=(), tool_ids=None):
    content = [{"type": "tool_use", "id": (tool_ids[i] if tool_ids else "tu-%s-%d" % (mid, i)), "name": n, "input": {}} for i, n in enumerate(tools)] or [{"type": "text", "text": "ok"}]
    u = dict(zip(("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"), usage))
    m = {"role": "assistant", "content": content, "usage": u}
    if mid is not None:
        m["id"] = mid
    return {"type": "assistant", "message": m}


def write_log(path, rows, extra=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "".join(e + "\n" for e in extra), encoding="utf-8")
    return path


# ---------------------------------------------------------------- trigger kinds

@pytest.mark.parametrize("text,kind", [
    ("<task-notification>\n<summary>Monitor event: \"watcher\"</summary>\n<event>[Monitor expired after 30m]</event>", "monitor expiry"),
    ("<task-notification>\n<summary>Monitor event: \"watcher\"</summary>\n<event>new line</event>", "monitor event"),
    ("<task-notification>\n<task-id>abc</task-id>\n<summary>Agent done</summary>", "task/agent notification"),
    ("This session is being continued from a previous conversation that ran out of context.", "session continuation"),
    ("<command-name>/clear</command-name>", "slash command"),
    ("please fix the build", ta.OTHER),
    ("", ta.OTHER),
    (None, ta.OTHER),
])
def test_builtin_kinds(text, kind):
    assert ta.kind_of(text) == kind


def test_markers_are_case_insensitive_and_only_the_head_counts():
    assert ta.kind_of("MONITOR EVENT: x") == "monitor event"
    assert ta.kind_of("x" * ta.HEAD + "Monitor event") == ta.OTHER


def test_the_builtin_labels_are_generic():
    assert [label for _m, label in ta.BUILTIN_RULES] == ["monitor expiry", "monitor event", "task/agent notification", "session continuation", "slash command"]


def test_own_rules_win_over_builtin_ones():
    rules = [("[nightly-check]", "scheduled prompt")]
    assert ta.kind_of("[nightly-check] <task-notification>", rules) == "scheduled prompt"
    assert ta.kind_of("<task-notification>", rules) == "task/agent notification"


# ---------------------------------------------------------------- the triggers file

def test_rules_file_missing_is_fine(tmp_path):
    assert ta.load_rules(tmp_path / "none.json") == ([], "")


def test_rules_file_is_read_with_labels_cut_and_bad_entries_skipped(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps([{"contains": "[Nightly]", "label": "x" * 100}, {"contains": "", "label": "empty"}, {"label": "no marker"}, 5, {"contains": 1, "label": "n"}]), encoding="utf-8")
    rules, problem = ta.load_rules(p)
    assert problem == "" and rules == [("[nightly]", "x" * 40)]


@pytest.mark.parametrize("content,word", [("{broken", "JSON"), ('{"a": 1}', "list")])
def test_rules_file_problems_are_reported_not_raised(tmp_path, content, word):
    p = tmp_path / "r.json"
    p.write_text(content, encoding="utf-8")
    rules, problem = ta.load_rules(p)
    assert rules == [] and word in problem and "ignored" in problem


def test_rules_file_too_big_or_with_bom(tmp_path):
    big = tmp_path / "big.json"
    big.write_text("[" + " " * 70000 + "]", encoding="utf-8")
    assert "64 KB" in ta.load_rules(big)[1]
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + json.dumps([{"contains": "zz", "label": "L"}]).encode())
    assert ta.load_rules(bom) == ([("zz", "L")], "")


def test_rules_are_capped(tmp_path):
    p = tmp_path / "many.json"
    p.write_text(json.dumps([{"contains": "m%d" % i, "label": "L"} for i in range(200)]), encoding="utf-8")
    assert len(ta.load_rules(p)[0]) == ta.MAX_RULES


def test_default_rules_file_follows_the_logs_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, "LOGS", tmp_path)
    (tmp_path / ta.TRIGGERS_NAME).write_text(json.dumps([{"contains": "zz", "label": "mine"}]), encoding="utf-8")
    assert ta.load_rules() == ([("zz", "mine")], "")


# ---------------------------------------------------------------- attributing usage

def test_usage_goes_to_the_kind_that_started_the_turn():
    rows = [user("first question"), asst("a", (1, 10, 100, 5)),
            user("<task-notification>\n<summary>Monitor event: w</summary>"), asst("b", (2, 20, 200, 6)), asst("c", (3, 30, 300, 7)),
            user("another question"), asst("d", (4, 40, 400, 8))]
    res = ta.audit(rows)
    k = res["by_kind"]
    assert res["calls"] == 4 and k[ta.OTHER]["calls"] == 2 and k["monitor event"]["calls"] == 2
    assert k["monitor event"]["input"] == 5 and k["monitor event"]["cache_read"] == 50 and k["monitor event"]["cache_write"] == 500 and k["monitor event"]["output"] == 13
    assert k[ta.OTHER]["cache_read"] == 50


def test_calls_before_any_user_text_are_attributed_to_start():
    assert list(ta.audit([asst("a")])["by_kind"]) == ["start"]


def test_a_streamed_message_is_counted_once_with_its_largest_usage():
    rows = [user("q"), asst("m1", (1, 1, 1, 1)), asst("m1", (1, 1, 1, 90)), asst("m1", (1, 1, 1, 5)), asst("m2", (0, 0, 0, 3))]
    res = ta.audit(rows)
    assert res["calls"] == 2 and res["by_kind"][ta.OTHER]["output"] == 93


def test_rows_without_a_message_id_are_each_counted():
    assert ta.audit([user("q"), asst(None), asst(None)])["calls"] == 2


def test_tool_results_do_not_change_the_current_trigger():
    rows = [user("<task-notification>x"), results("data"), asst("a")]
    assert list(ta.audit(rows)["by_kind"]) == ["task/agent notification"]


def test_a_user_row_with_text_and_a_result_does_change_the_trigger():
    row = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "r"}, {"type": "text", "text": "Monitor event: x"}]}}
    assert list(ta.audit([user("q"), row, asst("a")])["by_kind"]) == ["monitor event"]


def test_tools_are_counted_once_per_tool_use_id():
    rows = [asst("a", tools=("Bash",), tool_ids=["t1"]), asst("a", tools=("Bash",), tool_ids=["t1"]), asst("b", tools=("Read", "Bash"))]
    assert ta.audit(rows)["tools"] == {"Bash": 2, "Read": 1}


def test_tool_result_characters_and_images_are_measured():
    rows = [results("abcde", [{"type": "text", "text": "12345678"}, {"type": "image", "source": {}}, {"type": "other"}]),
            {"type": "user", "message": {"role": "user", "content": [{"type": "image", "source": {}}]}}]
    res = ta.audit(rows)
    assert res["tool_chars"] == 5 + 8 + ta.IMAGE_CHARS and res["images"] == 2


def test_odd_rows_do_not_break_the_audit():
    rows = [{}, {"message": "text"}, {"type": "assistant", "message": {"role": "assistant", "usage": "bad", "content": "x"}},
            {"type": "assistant", "message": {"role": "assistant", "id": "z", "usage": {"input_tokens": "many", "output_tokens": None}}},
            {"type": "user", "message": {"role": "user", "content": [None, 5, {"type": "tool_result", "content": {"x": 1}}]}}]
    res = ta.audit(rows)
    assert res["calls"] == 1 and res["tool_chars"] == 0


# ---------------------------------------------------------------- the report

def test_report_has_a_total_row_and_orders_by_cost():
    res = ta.audit([user("cheap"), asst("a", (1, 5, 0, 1)), user("<task-notification>"), asst("b", (1, 1000, 0, 1))])
    lines = ta.format_report(res).splitlines()
    assert lines[0].startswith("API calls: 2") and lines[1].split()[0] == "trigger"
    assert lines[2].startswith("task/agent notification") and lines[3].startswith(ta.OTHER)
    total = next(l for l in lines if l.startswith("TOTAL")).split()
    assert total[1:] == ["2", "1005", "0", "2", "2"]
    assert any(l.startswith("top tools:") for l in lines) and lines[-1].startswith("tool result volume chars:")


def test_report_of_an_empty_session():
    text = ta.format_report(ta.audit([]))
    assert "API calls: 0" in text and "TOTAL" in text


# ---------------------------------------------------------------- reading

def test_iter_rows_skips_junk_and_over_long_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, "MAX_LINE", 1500)
    big = json.dumps(user("z" * 4000))
    p = write_log(tmp_path / "s.jsonl", [user("one")], ["not json \"user\"", big, "[1, \"user\"]", json.dumps({"type": "progress"}), json.dumps(user("two"))])
    texts = [r["message"]["content"] for r in ta.iter_rows(str(p)) if r.get("type") == "user"]
    assert texts == ["one", "two"]


# ---------------------------------------------------------------- finding the newest transcript

@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    (cfg / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(ta, "ROOT", tmp_path / "assistant")
    monkeypatch.setattr(ta, "LOGS", tmp_path / "logs")
    work = tmp_path / "work dir"
    work.mkdir()
    monkeypatch.chdir(work)
    return {"projects": cfg / "projects", "work": work, "tmp": tmp_path}


def test_projects_dir_follows_claude_config_dir(env, monkeypatch):
    assert ta.projects_dir() == env["projects"]
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert ta.projects_dir() == Path.home() / ".claude" / "projects"


def test_latest_transcript_is_the_newest_top_level_file(env):
    folder = env["projects"] / ta.project_slug(env["work"].resolve())
    now = time.time()
    old = write_log(folder / "old.jsonl", [])
    new = write_log(folder / "new.jsonl", [])
    os.utime(old, (now - 500, now - 500))
    os.utime(new, (now - 5, now - 5))
    sub = write_log(folder / "sess" / "subagents" / "agent-1.jsonl", [])
    os.utime(sub, (now, now))
    assert ta.latest_transcript() == str(new)


def test_latest_transcript_for_a_named_project_or_a_subfolder(env):
    other = env["tmp"] / "other"
    (other / "deep").mkdir(parents=True)
    f = write_log(env["projects"] / ta.project_slug(other.resolve()) / "s.jsonl", [])
    assert ta.latest_transcript(str(other)) == str(f)
    assert ta.latest_transcript(str(other / "deep")) == str(f)
    assert ta.latest_transcript(str(env["tmp"] / "nowhere")) is None


def test_latest_transcript_none_when_nothing_exists(env):
    assert ta.latest_transcript() is None


# ---------------------------------------------------------------- the command

def test_main_with_a_file(env, capsys):
    p = write_log(env["tmp"] / "s.jsonl", [user("hello"), asst("a", (1, 2, 3, 4), tools=("Bash",))])
    assert ta.main([str(p)]) == 0
    out = capsys.readouterr().out
    assert "API calls: 1" in out and "top tools: [('Bash', 1)]" in out


def test_main_latest_uses_the_project_folder(env, capsys):
    write_log(env["projects"] / ta.project_slug(env["work"].resolve()) / "s.jsonl", [user("hello"), asst("a")])
    assert ta.main(["--latest"]) == 0
    assert "API calls: 1" in capsys.readouterr().out


def test_main_latest_with_nothing_found_exits_1(env, capsys):
    assert ta.main(["--latest"]) == 1
    assert "no Claude Code transcript found" in capsys.readouterr().out


def test_main_project_option(env, capsys):
    other = env["tmp"] / "picked"
    other.mkdir()
    write_log(env["projects"] / ta.project_slug(other.resolve()) / "s.jsonl", [user("hi"), asst("a"), asst("b")])
    assert ta.main(["--latest", "--project", str(other)]) == 0
    assert "API calls: 2" in capsys.readouterr().out


def test_main_uses_the_triggers_file(env, capsys):
    (env["tmp"] / "logs").mkdir()
    (env["tmp"] / "logs" / ta.TRIGGERS_NAME).write_text(json.dumps([{"contains": "[nightly]", "label": "scheduled prompt"}]), encoding="utf-8")
    p = write_log(env["tmp"] / "s.jsonl", [user("[nightly] run the check"), asst("a")])
    assert ta.main([str(p)]) == 0
    assert "scheduled prompt" in capsys.readouterr().out


def test_main_mentions_a_broken_triggers_file_and_carries_on(env, capsys):
    (env["tmp"] / "logs").mkdir()
    (env["tmp"] / "logs" / ta.TRIGGERS_NAME).write_text("{oops", encoding="utf-8")
    p = write_log(env["tmp"] / "s.jsonl", [user("hi"), asst("a")])
    assert ta.main([str(p)]) == 0
    cap = capsys.readouterr()
    assert "API calls: 1" in cap.out and "ignored" in cap.err


@pytest.mark.parametrize("argv", [[], ["a.jsonl", "--latest"], ["--bogus"]])
def test_main_bad_arguments_exit_2(env, argv, capsys):
    assert ta.main(argv) == 2


def test_main_missing_file_and_folder_exit_1(env, capsys):
    assert ta.main([str(env["tmp"] / "nope.jsonl")]) == 1
    assert ta.main([str(env["tmp"])]) == 1
    assert "not a file" in capsys.readouterr().out


def test_main_unreadable_file_exits_1(env, monkeypatch, capsys):
    p = write_log(env["tmp"] / "s.jsonl", [user("x")])

    def deny(path):
        raise PermissionError("no")
        yield  # pragma: no cover
    monkeypatch.setattr(ta, "iter_rows", deny)
    assert ta.main([str(p)]) == 1
    assert "cannot read" in capsys.readouterr().out


def test_main_empty_file_is_a_report_of_zero(env, capsys):
    p = env["tmp"] / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert ta.main([str(p)]) == 0
    assert "API calls: 0" in capsys.readouterr().out


# ---------------------------------------------------------------- as a real process

def run_script(args, tmp_path, cwd=None):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"),
               CLAUDE_CONFIG_DIR=str(tmp_path / "cfg"), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-B", str(HERE / "token_audit.py")] + args, env=env, cwd=cwd, capture_output=True, text=True, timeout=30)


def test_the_script_runs_as_a_process(tmp_path):
    p = write_log(tmp_path / "s.jsonl", [user("hi"), asst("a", (1, 2, 3, 4))])
    r = run_script([str(p)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "API calls: 1" in r.stdout and "TOTAL" in r.stdout


def test_the_script_without_arguments_fails_cleanly_instead_of_crashing(tmp_path):
    r = run_script([], tmp_path)
    assert r.returncode == 2 and "Traceback" not in r.stderr and "transcript file" in r.stderr


def test_the_script_finds_the_latest_transcript_of_the_current_folder(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    write_log(tmp_path / "cfg" / "projects" / ta.project_slug(work.resolve()) / "s.jsonl", [user("hi"), asst("a"), asst("b")])
    r = run_script(["--latest"], tmp_path, cwd=str(work))
    assert r.returncode == 0, r.stderr + r.stdout
    assert "API calls: 2" in r.stdout
