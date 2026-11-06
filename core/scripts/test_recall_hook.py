# -*- coding: utf-8 -*-
"""Tests for recall_hook.py. The search is a fake function everywhere except the two tests marked REAL SEARCH,
which run the kit's own memory_search.py against a tiny temporary vault to prove the line parsing matches it."""

import importlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import recall_hook


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp root with logs/ and a vault holding one note; the module is reloaded so its paths point there."""
    logs = tmp_path / "logs"
    logs.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Backups.md").write_text("The nightly backup script is sync_backup.py and it runs at 03:00 daily\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setenv("HARNESS_VAULT", str(vault))
    importlib.reload(recall_hook)
    return tmp_path, logs, vault


def fake_lines(*_a):
    return ["notes/Backups.md | 3.2000 | The nightly backup script is sync_backup.py"]


Q = "which script runs the nightly backup?"


# ---------------------------------------------------------------- extract_query

class TestExtractQuery:
    @pytest.mark.parametrize("prompt", [
        "[SYSTEM NOTIFICATION] a job finished",
        "<task-notification>done</task-notification>",
        "Work tick 14:05, nothing to do",
        "[Request interrupted by user]",
        "<system-reminder>be careful</system-reminder> what is the plan?",
        "[no-reply] what is the plan today?",
        "   [system notification] lower case works too",
    ])
    def test_automated_messages_give_nothing(self, prompt):
        assert recall_hook.extract_query(prompt) == ""

    def test_whitespace_collapsed_to_one_line(self):
        assert recall_hook.extract_query("what  is\n\tthe   plan?") == "what is the plan?"

    def test_capped_at_400_chars(self):
        assert len(recall_hook.extract_query("word " * 300)) == 400

    @pytest.mark.parametrize("prompt", [None, "", "   ", 42, ["x"]])
    def test_empty_or_odd_input_gives_empty(self, prompt):
        assert recall_hook.extract_query(prompt) == ""

    def test_unicode_kept(self):
        assert recall_hook.extract_query("what did we say about café 🎉?") == "what did we say about café 🎉?"


# ---------------------------------------------------------------- should_recall

class TestShouldRecall:
    def test_too_short_is_skipped(self):
        assert recall_hook.should_recall("what is it?") is False          # 11 chars

    def test_exactly_20_chars_passes_the_length_gate(self):
        q = "what is the plan??"                                          # build to 20
        q = q + "x" * (20 - len(q))
        assert len(q) == 20 and recall_hook.should_recall(q) is True

    def test_19_chars_is_skipped(self):
        q = "what is the plan???"[:19]
        assert len(q) == 19 and recall_hook.should_recall(q) is False

    def test_slash_command_is_skipped(self):
        assert recall_hook.should_recall("/compact what did we do last time?") is False

    @pytest.mark.parametrize("q", [
        "tell me the thing we agreed on yesterday?",
        "does the backup run every night",
        "Which folder holds the receipts",
        "  how do I restart the watcher",
        "...why did the build fail yesterday",
    ])
    def test_questions_trigger(self, q):
        assert recall_hook.should_recall(q) is True

    @pytest.mark.parametrize("q", [
        "please look at sync_backup.py for me",
        "like last time, do the same thing",
        "we decided to keep the old layout",
        "remember the plan for the trip",
        "that happened again this morning",
        "I want to continue from earlier today",
    ])
    def test_triggers_without_a_question_mark(self, q):
        assert recall_hook.should_recall(q) is True

    @pytest.mark.parametrize("q", [
        "ok thanks, that looks good to me",
        "please add the new button to the page",
        "make the header a bit bigger now",
    ])
    def test_plain_statements_are_skipped(self, q):
        assert recall_hook.should_recall(q) is False

    @pytest.mark.parametrize("q", [None, "", 7])
    def test_odd_input_is_false(self, q):
        assert recall_hook.should_recall(q) is False


# ---------------------------------------------------------------- format_block

class TestFormatBlock:
    @pytest.mark.parametrize("lines", [None, [], [""], ["no separators here"], ["a | b"], ["a | 1.0 | "]])
    def test_nothing_usable_gives_empty_string(self, lines):
        assert recall_hook.format_block(lines) == ""

    def test_header_folder_and_extension_dropped(self):
        out = recall_hook.format_block(["Projects\\Web\\Plan.md | 4.1000 | ship on friday"])
        assert out.startswith(recall_hook.HEADER + "\n")
        assert out.splitlines()[1] == "- Plan: ship on friday"

    def test_forward_slash_paths_too(self):
        out = recall_hook.format_block(["a/b/Notes.md | 1.0 | hello there"])
        assert out.splitlines()[1] == "- Notes: hello there"

    def test_non_md_name_kept_whole(self):
        out = recall_hook.format_block(["a/readme.txt | 1.0 | hi"])
        assert out.splitlines()[1] == "- readme.txt: hi"

    def test_each_text_cut_at_230(self):
        out = recall_hook.format_block(["n.md | 1.0 | " + "x" * 500])
        assert out.splitlines()[1] == "- n: " + "x" * 230

    def test_pipes_inside_the_text_survive(self):
        out = recall_hook.format_block(["t.md | 2.0 | a | b | c"])
        assert out.splitlines()[1] == "- t: a | b | c"

    def test_total_is_capped(self):
        lines = ["n%d.md | 1.0 | %s" % (i, "y" * 200) for i in range(20)]
        out = recall_hook.format_block(lines)
        items = out.splitlines()[1:]
        assert 0 < len(items) < 20
        assert sum(len(i) for i in items) <= recall_hook.MAX_CHARS

    def test_custom_small_cap_keeps_whole_items_only(self):
        lines = ["a.md | 1.0 | " + "z" * 100, "b.md | 1.0 | short"]
        assert recall_hook.format_block(lines, max_chars=50) == ""        # the first item alone is over the cap

    def test_malformed_and_non_string_entries_skipped(self):
        out = recall_hook.format_block(["garbage", None, 5, "ok.md | 1.0 | fine"])
        assert out.splitlines()[1:] == ["- ok: fine"]


# ---------------------------------------------------------------- run

class TestRun:
    def test_injected_search_result_becomes_the_block(self, env):
        _root, logs, vault = env
        out = recall_hook.run(Q, logs=logs, search=fake_lines, vault=vault)
        assert out.startswith("[RECALL] vault lines that may answer this")
        assert "- Backups: The nightly backup script is sync_backup.py" in out

    def test_the_search_gets_the_cleaned_query(self, env):
        _root, logs, vault = env
        seen = []
        recall_hook.run("which   script\nruns the nightly backup?", logs=logs, search=lambda q: seen.append(q) or [], vault=vault)
        assert seen == ["which script runs the nightly backup?"]

    def test_kill_switch_stops_everything(self, env):
        _root, logs, vault = env
        (logs / "recall_off.txt").write_text("")
        calls = []
        out = recall_hook.run(Q, logs=logs, search=lambda q: calls.append(q) or fake_lines(), vault=vault)
        assert out == "" and calls == []
        assert not (logs / "recall_log.jsonl").exists()

    def test_kill_switch_removed_works_again(self, env):
        _root, logs, vault = env
        off = logs / "recall_off.txt"
        off.write_text("")
        assert recall_hook.run(Q, logs=logs, search=fake_lines, vault=vault) == ""
        off.unlink()
        assert recall_hook.run(Q, logs=logs, search=fake_lines, vault=vault) != ""

    def test_empty_vault_prints_nothing_and_never_searches(self, env, tmp_path):
        _root, logs, _vault = env
        empty = tmp_path / "emptyvault"
        empty.mkdir()
        (empty / "notes.txt").write_text("not markdown")
        calls = []
        out = recall_hook.run(Q, logs=logs, search=lambda q: calls.append(q) or fake_lines(), vault=empty)
        assert out == "" and calls == []

    def test_missing_vault_prints_nothing_and_never_searches(self, env, tmp_path):
        _root, logs, _vault = env
        calls = []
        out = recall_hook.run(Q, logs=logs, search=lambda q: calls.append(q) or fake_lines(), vault=tmp_path / "nope")
        assert out == "" and calls == []

    def test_obsidian_folder_alone_is_not_an_index(self, env, tmp_path):
        _root, logs, _vault = env
        v = tmp_path / "v2"
        (v / ".obsidian").mkdir(parents=True)
        (v / ".obsidian" / "x.md").write_text("config")
        assert recall_hook.has_index(v) is False

    def test_automated_message_is_skipped_without_searching_or_logging(self, env):
        _root, logs, vault = env
        calls = []
        out = recall_hook.run("[SYSTEM NOTIFICATION] what is the status of the backup job?", logs=logs,
                              search=lambda q: calls.append(q) or fake_lines(), vault=vault)
        assert out == "" and calls == []
        assert not (logs / "recall_log.jsonl").exists()

    def test_statement_is_skipped_without_searching(self, env):
        _root, logs, vault = env
        calls = []
        out = recall_hook.run("please add the new button to the page", logs=logs,
                              search=lambda q: calls.append(q) or fake_lines(), vault=vault)
        assert out == "" and calls == []

    def test_a_log_line_is_written_per_run(self, env):
        _root, logs, vault = env
        out = recall_hook.run(Q, logs=logs, search=fake_lines, vault=vault)
        recall_hook.run(Q + " again", logs=logs, search=fake_lines, vault=vault)
        rows = [json.loads(x) for x in (logs / "recall_log.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 2
        r = rows[0]
        assert set(r) == {"ts", "q", "ms", "lines", "chars"}
        assert r["q"] == Q and r["lines"] == 1 and r["chars"] == len(out) and isinstance(r["ms"], int)

    def test_logged_query_is_cut_at_120(self, env):
        _root, logs, vault = env
        recall_hook.run("what is " + "long " * 100 + "?", logs=logs, search=fake_lines, vault=vault)
        row = json.loads((logs / "recall_log.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert len(row["q"]) == 120

    def test_logs_folder_is_created_when_missing(self, env, tmp_path):
        _root, _logs, vault = env
        fresh = tmp_path / "fresh" / "logs"
        recall_hook.run(Q, logs=fresh, search=fake_lines, vault=vault)
        assert (fresh / "recall_log.jsonl").exists()

    def test_search_that_returns_nothing_gives_empty_block_but_is_logged(self, env):
        _root, logs, vault = env
        assert recall_hook.run(Q, logs=logs, search=lambda q: [], vault=vault) == ""
        row = json.loads((logs / "recall_log.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert row["lines"] == 0 and row["chars"] == 0

    def test_search_that_raises_gives_empty_string_not_an_error(self, env):
        _root, logs, vault = env

        def boom(_q):
            raise RuntimeError("search exploded")
        assert recall_hook.run(Q, logs=logs, search=boom, vault=vault) == ""

    def test_unwritable_log_does_not_lose_the_block(self, env, tmp_path):
        _root, _logs, vault = env
        blocker = tmp_path / "afile"
        blocker.write_text("i am a file, not a folder")
        out = recall_hook.run(Q, logs=blocker / "logs", search=fake_lines, vault=vault)
        assert "Backups" in out

    @pytest.mark.parametrize("prompt", [None, 12, {"a": 1}, b"bytes"])
    def test_run_never_raises_on_odd_prompts(self, env, prompt):
        _root, logs, vault = env
        assert recall_hook.run(prompt, logs=logs, search=fake_lines, vault=vault) == ""

    def test_timeout_in_the_real_search_function_means_nothing(self, env, monkeypatch):
        _root, logs, vault = env

        def slow(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=4)
        monkeypatch.setattr(subprocess, "run", slow)
        assert recall_hook.run(Q, logs=logs, vault=vault) == ""


# ---------------------------------------------------------------- main / CLI

def _stdin(monkeypatch, data):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))


class TestMain:
    def test_hook_payload_prints_the_block(self, env, monkeypatch, capsys):
        _stdin(monkeypatch, json.dumps({"prompt": Q}).encode("utf-8"))
        monkeypatch.setattr(recall_hook, "search_vault", lambda q, v=None: fake_lines())
        assert recall_hook.main([]) == 0
        out = capsys.readouterr().out
        assert out.startswith("[RECALL]") and "sync_backup.py" in out

    def test_unicode_prompt_bytes_are_read_as_utf8(self, env, monkeypatch, capsys):
        seen = []
        monkeypatch.setattr(recall_hook, "search_vault", lambda q, v=None: seen.append(q) or fake_lines())
        _stdin(monkeypatch, json.dumps({"prompt": "what did we decide about the café menu 🎉?"}, ensure_ascii=False).encode("utf-8"))
        recall_hook.main([])
        assert seen == ["what did we decide about the café menu 🎉?"]

    @pytest.mark.parametrize("data", [b"", b"   ", b"not json at all", b"\xff\xfe\x00 garbage", b"[1, 2, 3]", b'"just a string"', b'{"prompt": null}'])
    def test_garbage_stdin_prints_nothing_and_does_not_raise(self, env, monkeypatch, capsys, data):
        _stdin(monkeypatch, data)
        monkeypatch.setattr(recall_hook, "search_vault", lambda q, v=None: fake_lines())
        assert recall_hook.main([]) == 0
        assert capsys.readouterr().out == ""

    def test_non_question_prompt_prints_nothing(self, env, monkeypatch, capsys):
        _stdin(monkeypatch, json.dumps({"prompt": "please add the new button to the page"}).encode("utf-8"))
        monkeypatch.setattr(recall_hook, "search_vault", lambda q, v=None: fake_lines())
        recall_hook.main([])
        assert capsys.readouterr().out == ""

    def test_kill_switch_through_main(self, env, monkeypatch, capsys):
        _root, logs, _vault = env
        (logs / "recall_off.txt").write_text("")
        _stdin(monkeypatch, json.dumps({"prompt": Q}).encode("utf-8"))
        monkeypatch.setattr(recall_hook, "search_vault", lambda q, v=None: fake_lines())
        recall_hook.main([])
        assert capsys.readouterr().out == ""

    def test_main_never_raises_even_if_run_blows_up(self, env, monkeypatch, capsys):
        _stdin(monkeypatch, json.dumps({"prompt": Q}).encode("utf-8"))

        def boom(*a, **k):
            raise RuntimeError("everything is broken")
        monkeypatch.setattr(recall_hook, "run", boom)
        assert recall_hook.main([]) == 0
        assert capsys.readouterr().out == ""

    def test_test_mode_prints_what_would_be_injected(self, env, monkeypatch, capsys):
        monkeypatch.setattr(recall_hook, "search_vault", lambda q, v=None: fake_lines())
        assert recall_hook.main(["--test", "which", "script", "runs", "the", "nightly", "backup?"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("[RECALL]") and "sync_backup.py" in out

    def test_test_mode_says_so_when_nothing_is_injected(self, env, monkeypatch, capsys):
        monkeypatch.setattr(recall_hook, "search_vault", lambda q, v=None: fake_lines())
        recall_hook.main(["--test", "ok"])
        assert capsys.readouterr().out.strip() == "(nothing injected)"


# ---------------------------------------------------------------- REAL SEARCH (the kit's own memory_search.py)

class TestRealSearch:
    def test_REAL_SEARCH_output_parses_into_a_block(self, env):
        _root, logs, vault = env
        lines = recall_hook.search_vault("which script runs the nightly backup?", vault)
        assert lines, "memory_search.py returned nothing for a query that matches the temp note"
        assert any(" | " in ln for ln in lines)
        block = recall_hook.format_block(lines)
        assert block.startswith("[RECALL]") and "- Backups: " in block and "sync_backup.py" in block

    def test_REAL_SEARCH_through_run_end_to_end(self, env):
        _root, logs, vault = env
        out = recall_hook.run("which script runs the nightly backup?", logs=logs, vault=vault)
        assert "sync_backup.py" in out
        assert (logs / "recall_log.jsonl").exists()

    def test_search_failure_returns_empty_list(self, env, monkeypatch):
        _root, _logs, vault = env
        monkeypatch.setattr(recall_hook, "SEARCH_SCRIPT", Path(vault) / "does_not_exist.py")
        assert recall_hook.search_vault("which script runs the nightly backup?", vault) == []

    def test_query_starting_with_a_dash_is_not_lost(self, env):
        _root, _logs, vault = env
        assert recall_hook.search_vault("--which script runs the nightly backup?", vault)

    def test_empty_query_returns_empty_list(self, env):
        _root, _logs, vault = env
        assert recall_hook.search_vault("", vault) == [] and recall_hook.search_vault("--", vault) == []
