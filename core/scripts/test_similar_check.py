# -*- coding: utf-8 -*-
"""Tests for similar_check.py: the similarity helpers, what counts as a new file, the messages, the log, the kill switch and the real process."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import similar_check as sc  # noqa: E402

DOC_A = '"""Moves finished weekly reports older than thirty days out of the active reports file into an archive note, dry run by default, tracking first seen timestamps."""\nx = 1\n'
DOC_B = '"""Moves finished weekly reports that sat in the active reports file for thirty days into an archive note, dry run by default, tracking first seen dates."""\nx = 1\n'
DOC_C = '"""Renders an invoice pdf with the letterhead centred, items numbered and the total written in words."""\nx = 1\n'


@pytest.fixture
def area(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "vault" / "05 - Resources").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    return tmp_path


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def hook(area, path, response=None, **extra):
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(path)}, "tool_response": response if response is not None else {"type": "create"}}
    payload.update(extra)
    return sc.handle(payload, logs=area / "logs", dirs=[area / "scripts"], vault=area / "vault")


def test_words_and_jaccard():
    assert sc.words("The Finished Reports archive") == {"finished", "reports", "archive"}
    assert sc.jaccard({"a"}, {"a"}) == 1.0 and sc.jaccard(set(), {"a"}) == 0.0
    assert sc.jaccard({"a", "b"}, {"a", "c"}) == 1 / 3


def test_jaccard_is_symmetric():
    assert sc.jaccard({"a", "b"}, {"a", "c"}) == sc.jaccard({"a", "c"}, {"a", "b"})


def test_similar_scripts_finds_the_near_duplicate_and_not_the_unrelated(tmp_path):
    put(tmp_path / "archive_reports.py", DOC_A)
    put(tmp_path / "make_invoice.py", DOC_C)
    new = put(tmp_path / "archive_reports_new.py", DOC_B)
    assert [f for _, f in sc.similar_scripts(str(new), str(tmp_path))] == ["archive_reports.py"]


def test_a_script_is_never_similar_to_itself(tmp_path):
    new = put(tmp_path / "only.py", DOC_A)
    assert sc.similar_scripts(str(new), str(tmp_path)) == []


def test_test_files_are_not_candidates(tmp_path):
    put(tmp_path / "test_archive_reports.py", DOC_A)
    new = put(tmp_path / "archive_reports_new.py", DOC_B)
    assert sc.similar_scripts(str(new), str(tmp_path)) == []


def test_a_tiny_docstring_is_not_judged(tmp_path):
    put(tmp_path / "a.py", DOC_A)
    new = put(tmp_path / "b.py", '"""Short."""\n')
    assert sc.similar_scripts(str(new), str(tmp_path)) == []


def test_a_script_without_a_docstring_or_with_a_syntax_error_is_not_judged(tmp_path):
    put(tmp_path / "a.py", DOC_A)
    assert sc.similar_scripts(str(put(tmp_path / "b.py", "x = 1\n")), str(tmp_path)) == []
    assert sc.similar_scripts(str(put(tmp_path / "c.py", "def f(:\n")), str(tmp_path)) == []


def test_a_missing_scripts_folder_gives_no_hits(tmp_path):
    new = put(tmp_path / "b.py", DOC_B)
    assert sc.similar_scripts(str(new), str(tmp_path / "nope")) == []
    assert sc.script_hits("Weekly Reports Archive", str(tmp_path / "nope")) == []


def test_similar_notes_by_title():
    titles = ["Finished Reports Archive", "Toolbox", "Experiment Log", "Invoice Pipeline Method"]
    out = sc.similar_notes("Weekly Reports - Finished Archive", titles)
    assert out and out[0][1] == "Finished Reports Archive"
    assert sc.similar_notes("Toolbox Two", titles) == []                     # only one meaningful word: not judged


def test_a_note_is_not_similar_to_its_own_title():
    assert sc.similar_notes("Finished Reports Archive", ["Finished Reports Archive"]) == []


def test_script_hits_finds_the_tool_that_already_does_it(tmp_path):
    put(tmp_path / "archive_reports.py", DOC_A)
    put(tmp_path / "make_invoice.py", DOC_C)
    assert [f for _, f in sc.script_hits("Weekly Reports Archive", str(tmp_path))] == ["archive_reports.py"]
    assert sc.script_hits("Garden Hose Types", str(tmp_path)) == []


def test_is_new_file_trusts_the_tool_response(tmp_path):
    f = put(tmp_path / "x.py", "x = 1\n")
    assert sc.is_new_file(str(f), {"type": "create"}) is True
    assert sc.is_new_file(str(f), {"type": "update"}) is False
    old = time.time() + 3600                                  # file "created" long before `now`
    assert sc.is_new_file(str(f), {"type": "update"}, now=old) is False


def test_is_new_file_falls_back_to_the_age_of_the_file(tmp_path):
    f = put(tmp_path / "x.py", "x = 1\n")
    assert sc.is_new_file(str(f), None) is True
    assert sc.is_new_file(str(f), "some text", now=time.time() + 3600) is False
    assert sc.is_new_file(str(tmp_path / "missing.py"), None) is False


def test_an_update_whose_text_mentions_create_is_not_a_create(area):
    new = put(area / "scripts" / "archive_reports_new.py", DOC_B)
    put(area / "scripts" / "archive_reports.py", DOC_A)
    resp = {"type": "update", "content": "this file will create things"}
    assert hook(area, new, resp) == ""


def test_new_script_with_a_near_duplicate_gets_one_line(area):
    put(area / "scripts" / "archive_reports.py", DOC_A)
    new = put(area / "scripts" / "archive_reports_new.py", DOC_B)
    out = hook(area, new)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "archive_reports.py" in ctx and "script_index.py" in ctx and "\n" not in ctx
    row = json.loads((area / "logs" / "similar_check.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["file"] == "archive_reports_new.py" and "archive_reports.py" in row["msg"]


def test_new_script_with_nothing_similar_is_silent_and_not_logged(area):
    put(area / "scripts" / "make_invoice.py", DOC_C)
    new = put(area / "scripts" / "archive_reports_new.py", DOC_B)
    assert hook(area, new) == ""
    assert not (area / "logs" / "similar_check.jsonl").exists()


def test_a_new_test_file_or_a_script_outside_the_scripts_folder_is_not_checked(area):
    put(area / "scripts" / "archive_reports.py", DOC_A)
    assert hook(area, put(area / "scripts" / "test_archive_reports_new.py", DOC_B)) == ""
    assert hook(area, put(area / "other" / "archive_reports_new.py", DOC_B)) == ""


def test_new_vault_note_with_a_similar_title_and_a_matching_script(area):
    put(area / "vault" / "05 - Resources" / "Finished Reports Archive.md", "# archive\n")
    put(area / "scripts" / "archive_reports.py", DOC_A)
    new = put(area / "vault" / "05 - Resources" / "Weekly Reports - Finished Archive.md", "# new\n")
    ctx = json.loads(hook(area, new))["hookSpecificOutput"]["additionalContext"]
    assert "Finished Reports Archive" in ctx and "archive_reports.py" in ctx


def test_new_vault_note_with_a_unique_title_is_silent(area):
    put(area / "vault" / "05 - Resources" / "Finished Reports Archive.md", "# archive\n")
    assert hook(area, put(area / "vault" / "05 - Resources" / "Garden Watering Plan.md", "# plants\n")) == ""


def test_a_markdown_file_outside_the_vault_is_not_a_note(area):
    put(area / "vault" / "05 - Resources" / "Finished Reports Archive.md", "# archive\n")
    assert hook(area, put(area / "docs" / "Finished Reports Archive Copy.md", "# x\n")) == ""


def test_note_titles_skip_hidden_folders_and_non_notes(area):
    put(area / "vault" / ".obsidian" / "Hidden Note.md", "x")
    put(area / "vault" / "A.txt", "x")
    put(area / "vault" / "05 - Resources" / "Shown.md", "x")
    assert sc.note_titles(area / "vault") == ["Shown"]


def test_sub_agent_calls_are_skipped(area):
    put(area / "scripts" / "archive_reports.py", DOC_A)
    new = put(area / "scripts" / "archive_reports_new.py", DOC_B)
    assert hook(area, new, agent_id="abc") == ""
    assert hook(area, new, agent_type="researcher") == ""


def test_other_tools_are_ignored(area):
    put(area / "scripts" / "archive_reports.py", DOC_A)
    new = put(area / "scripts" / "archive_reports_new.py", DOC_B)
    assert hook(area, new, tool_name="Edit") == ""


def test_kill_switch(area):
    put(area / "scripts" / "archive_reports.py", DOC_A)
    new = put(area / "scripts" / "archive_reports_new.py", DOC_B)
    (area / "logs" / sc.OFF_NAME).write_text("off", encoding="utf-8")
    assert hook(area, new) == ""


def test_garbage_payloads_never_raise(area):
    for payload in ({}, {"tool_name": "Write"}, {"tool_input": 5}, {"tool_input": {"file_path": 7}}, {"tool_input": {"file_path": str(area / "nope.py")}}, [], None, "x"):
        assert sc.handle(payload, logs=area / "logs", dirs=[area / "scripts"], vault=area / "vault") == ""


def test_the_log_is_rotated_when_large(area, monkeypatch):
    monkeypatch.setattr(sc, "LOG_ROTATE_BYTES", 10)
    sc.log_hit(area / "logs", "a.py", "first message")
    sc.log_hit(area / "logs", "b.py", "second message")
    assert (area / "logs" / "similar_check.jsonl.1").exists()
    assert "second" in (area / "logs" / "similar_check.jsonl").read_text(encoding="utf-8")


def test_paths_follow_the_environment_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.delenv("HARNESS_VAULT", raising=False)
    monkeypatch.delenv("HARNESS_LOGS", raising=False)
    assert sc.vault_dir() == tmp_path / "vault" and sc.logs_dir() == tmp_path / "logs"
    assert tmp_path / "scripts" in sc.scripts_dirs()


def run_hook(tmp_path, stdin_bytes):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, str(HERE / "similar_check.py")], input=stdin_bytes, capture_output=True, env=env, timeout=30)


def test_real_process_names_the_near_duplicate(area):
    put(area / "scripts" / "archive_reports.py", DOC_A)
    new = put(area / "scripts" / "archive_reports_new.py", DOC_B)
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": str(new)}, "tool_response": {"type": "create"}}
    r = run_hook(area, json.dumps(payload).encode("utf-8"))
    assert r.returncode == 0
    assert "archive_reports.py" in json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]


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
    assert sc.read_payload() == {}
    monkeypatch.setattr(sys, "stdin", None)
    assert sc.read_payload() == {}
