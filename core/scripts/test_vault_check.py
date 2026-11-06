# -*- coding: utf-8 -*-
"""Tests for vault_check.py (the worker's draft was rewritten in review: it had a syntax error on Python 3.10/3.11, printed no line for orphans, never checked the top-level
index or the 'Daily Notes' folder, and wrote a non-numeric sensor_last.txt). Real files in a temp folder; nothing mocked."""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import intent_store  # noqa: E402
import vault_check as vc  # noqa: E402

OK = "---\nstatus: active\n---\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "vault").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    return tmp_path


def note(env, rel, text=OK + "# x\n"):
    p = env / "vault" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def index(env, *links, extra=""):
    note(env, "VAULT-INDEX.md", "---\nstatus: active\ntype: index\n---\n# Index\n" + "\n".join("- [[%s]]" % l for l in links) + "\n" + extra)


def findings(env):
    return vc.check_vault(vc.Vault(env / "vault"))


def only(f, kind):
    return f[kind]


def all_empty(f):
    return all(v == [] for v in f.values())


# ---------- a clean vault ----------
def test_a_clean_vault_has_no_findings_and_exits_0(env, capsys):
    index(env, "A", "B")
    note(env, "A.md", OK + "see [[B]]\n")
    note(env, "B.md", OK + "see [[A]]\n")
    assert all_empty(findings(env))
    assert vc.main([]) == 0
    assert "summary: 3 notes, 0 finding(s)" in capsys.readouterr().out


def test_an_empty_vault_is_clean_and_a_missing_vault_exits_2(env, capsys, monkeypatch):
    assert vc.main([]) == 0 and "summary: 0 notes, 0 finding(s)" in capsys.readouterr().out
    monkeypatch.setenv("HARNESS_VAULT", str(env / "nowhere"))
    assert vc.main([]) == 2 and "vault folder not found" in capsys.readouterr().out


# ---------- broken links ----------
def test_a_broken_link_names_the_note_and_the_target(env):
    index(env, "A")
    note(env, "A.md", OK + "see [[Missing Note]]\n")
    assert only(findings(env), "broken_link") == ["A.md -> [[Missing Note]]"]


@pytest.mark.parametrize("link", ["B", "b", "B|shown text", "B#A heading", "B#^block|alias", "sub/B", "B.md", "[[B]]".strip("[]"), "B\\"])
def test_these_link_forms_all_resolve(env, link):
    index(env, "A", "B")
    note(env, "A.md", OK + "[[%s]]\n" % link)
    note(env, "sub/B.md")
    assert only(findings(env), "broken_link") == []


def test_a_link_to_a_folder_and_to_an_attachment_resolves(env):
    index(env, "A")
    note(env, "A.md", OK + "[[Projects]] and ![[photo.png]] and [[CLAUDE]]\n")
    (env / "vault" / "Projects").mkdir()
    (env / "vault" / "photo.png").write_bytes(b"png")
    assert only(findings(env), "broken_link") == []


def test_a_link_to_a_missing_attachment_is_broken(env):
    index(env, "A")
    note(env, "A.md", OK + "![[gone.png]]\n")
    assert only(findings(env), "broken_link") == ["A.md -> [[gone.png]]"]


def test_links_inside_code_are_ignored(env):
    index(env, "A")
    note(env, "A.md", OK + "inline `[[nope]]` and ``[[nope2]]`` here\n```\n[[nope3]]\n```\n~~~\n[[nope4]]\n~~~\n[[real]]\n")
    assert only(findings(env), "broken_link") == ["A.md -> [[real]]"]


def test_a_fence_closes_only_with_the_same_character_and_at_least_the_same_length(env):
    index(env, "A")
    note(env, "A.md", OK + "```\n[[a]]\n~~~\n[[b]]\n```\n[[c1]]\n````\n[[d]]\n```\n[[e]]\n````\n[[c2]]\n")
    assert only(findings(env), "broken_link") == ["A.md -> [[c1]]", "A.md -> [[c2]]"]


def test_triple_backticks_inside_a_line_are_inline_code_not_a_fence(env):
    index(env, "A")
    note(env, "A.md", OK + "```[[nope]]``` more\n[[real]]\n")
    assert only(findings(env), "broken_link") == ["A.md -> [[real]]"]


def test_an_unclosed_fence_hides_the_rest_like_a_markdown_viewer(env):
    index(env, "A")
    note(env, "A.md", OK + "```\n[[hidden]]\n")
    assert only(findings(env), "broken_link") == []


def test_a_link_with_an_escaped_pipe_in_a_table_uses_the_target_part(env):
    index(env, "A", "B")
    note(env, "A.md", OK + "| x |\n| [[B\\|shown]] |\n")
    note(env, "B.md")
    assert only(findings(env), "broken_link") == []


def test_unicode_note_names_and_links(env):
    index(env, "Café ☕")
    note(env, "Café ☕.md", OK + "[[café ☕]] and [[日本語]]\n")
    assert only(findings(env), "broken_link") == ["Café ☕.md -> [[日本語]]"]


def test_notes_with_bytes_that_are_not_utf8_are_read_with_replacement_not_crashed_on(env):
    index(env, "A")
    (env / "vault" / "A.md").write_bytes(b"---\nstatus: active\n---\nbad \xff\xfe bytes [[nowhere]]\n")
    assert only(findings(env), "broken_link") == ["A.md -> [[nowhere]]"]


def test_hidden_folders_are_not_part_of_the_vault(env):
    index(env, "A")
    note(env, "A.md")
    note(env, ".obsidian/workspace.md", "[[junk]]")
    f = findings(env)
    assert all_empty(f) and vc.Vault(env / "vault").notes.keys() == {"vault-index", "a"}


# ---------- orphans ----------
def test_an_orphan_is_a_note_nobody_links_to_and_no_index_lists(env):
    index(env, "A")
    note(env, "A.md")
    note(env, "Lonely.md")
    f = findings(env)
    assert only(f, "orphan") == ["Lonely.md"] and only(f, "missing_from_index") == ["Lonely.md"]


def test_a_note_linked_from_another_note_is_not_an_orphan_but_is_still_unlisted(env):
    index(env, "A")
    note(env, "A.md", OK + "[[Hidden]]\n")
    note(env, "Hidden.md")
    f = findings(env)
    assert only(f, "orphan") == [] and only(f, "missing_from_index") == ["Hidden.md"]


def test_a_link_to_itself_does_not_save_an_orphan(env):
    index(env, "A")
    note(env, "A.md")
    note(env, "Self.md", OK + "[[Self]]\n")
    assert only(findings(env), "orphan") == ["Self.md"]


def test_a_note_named_only_as_a_file_name_in_the_index_is_listed_and_not_an_orphan(env):
    index(env, "A", extra="- `Later.md`: created by a later step\n")
    note(env, "A.md")
    note(env, "Later.md")
    f = findings(env)
    assert only(f, "orphan") == [] and only(f, "missing_from_index") == []


def test_daily_notes_templates_underscore_files_and_archive_are_not_orphans_or_unlisted_or_status_checked(env):
    index(env, "A")
    note(env, "A.md")
    note(env, "01 - Daily Notes/2026-10-31.md", "---\ntype: daily\n---\n# day\n")
    note(env, "01 - Daily Notes/Daily Note Template.md", "# template, no frontmatter\n")
    note(env, "_decoy.md", "x")
    note(env, "04 - Archive/Old.md", "no frontmatter")
    assert all_empty(findings(env))


def test_a_template_anywhere_and_a_note_in_a_daily_folder_need_no_frontmatter_link_or_listing(env):
    index(env, "A")
    note(env, "A.md")
    note(env, "00 - Inbox/Meeting Template.md", "no frontmatter at all\n")
    note(env, "01 - Daily Notes/2026-10-31.md", "no frontmatter at all\n")
    assert all_empty(findings(env))


def test_a_folder_with_daily_in_its_name_is_checked_for_date_names_even_without_frontmatter(env):
    index(env, "A")
    note(env, "A.md")
    note(env, "01 - Daily Notes/monday.md", "no frontmatter at all\n")
    assert only(findings(env), "bad_daily_name") == ["01 - Daily Notes/monday.md"]


def test_the_type_value_is_read_case_insensitively(env):
    note(env, "VAULT-INDEX.md", "---\nstatus: active\ntype: Index\n---\n- [[A]]\n")
    note(env, "A.md")
    note(env, "Journal/today.md", "---\ntype: Daily\n---\n")
    f = findings(env)
    assert only(f, "missing_index") == [] and only(f, "bad_daily_name") == ["Journal/today.md"]


def test_the_boot_file_byte_order_mark_before_the_first_dashes_is_allowed(env):
    index(env, "A")
    (env / "vault" / "A.md").write_bytes("\N{ZERO WIDTH NO-BREAK SPACE}---\nstatus: active\n---\n".encode("utf-8"))
    assert only(findings(env), "missing_status") == []


# ---------- the index ----------
def test_a_note_missing_from_the_index_is_found(env):
    index(env, "A")
    note(env, "A.md")
    note(env, "02 - Projects/Plan.md", OK + "[[A]]\n")
    assert only(findings(env), "missing_from_index") == ["02 - Projects/Plan.md"]


@pytest.mark.parametrize("line", ["- [[Plan]]", "- [Plan](02%20-%20Projects/Plan.md)", "- `Plan.md`", "- [[02 - Projects/Plan|the plan]]"])
def test_every_way_of_listing_a_note_counts(env, line):
    note(env, "VAULT-INDEX.md", "---\nstatus: active\ntype: index\n---\n" + line + "\n")
    note(env, "02 - Projects/Plan.md")
    assert only(findings(env), "missing_from_index") == []


def test_a_folder_index_note_lists_its_own_notes(env):
    index(env, "Projects")
    note(env, "Projects.md", OK.replace("active\n", "active\ntype: index\n") + "[[Plan]]\n")
    note(env, "02 - Projects/Plan.md")
    note(env, "02 - Projects/Other.md")
    assert only(findings(env), "missing_from_index") == ["02 - Projects/Other.md"]


def test_no_index_at_all_is_one_finding_and_the_notes_are_not_each_reported_unlisted(env):
    note(env, "A.md")
    f = findings(env)
    assert len(only(f, "missing_index")) == 1 and only(f, "missing_from_index") == []


def test_an_index_markdown_link_to_a_note_that_does_not_exist_is_found(env):
    index(env, "A", extra="- [Gone](Gone.md) and [Here](A.md)\n")
    note(env, "A.md")
    assert only(findings(env), "broken_index_link") == ["VAULT-INDEX.md -> Gone.md"]


def test_a_code_span_entry_for_a_note_not_made_yet_is_not_a_broken_index_link(env):
    index(env, "A", extra="- `Experiment Log.md` (created when first needed)\n")
    note(env, "A.md")
    assert all_empty(findings(env))


# ---------- frontmatter ----------
@pytest.mark.parametrize("text,why", [("# no frontmatter\n", "no frontmatter"), ("---\ntitle: x\n---\n# x\n", "no status"), ("---\nstatus:\n---\n# x\n", "no status"),
                                      ("---\nstatus: active\n# never closed\n", "no frontmatter")])
def test_a_note_without_a_status_is_found(env, text, why):
    index(env, "A")
    note(env, "A.md", text)
    assert only(findings(env), "missing_status") == ["A.md (%s)" % why]


def test_frontmatter_with_a_byte_order_mark_and_windows_line_endings_is_read(env):
    index(env, "A")
    (env / "vault" / "A.md").write_bytes(b"\xef\xbb\xbf---\r\nstatus: active\r\n---\r\n# x\r\n")
    assert only(findings(env), "missing_status") == []


# ---------- daily notes ----------
@pytest.mark.parametrize("name,bad", [("2026-10-31", False), ("2026-13-45", True), ("2026-1-5", True), ("monday", True), ("Daily Note Template", False)])
def test_daily_note_names(env, name, bad):
    index(env, "A")
    note(env, "A.md")
    note(env, "01 - Daily Notes/%s.md" % name, "---\ntype: daily\n---\n")
    assert only(findings(env), "bad_daily_name") == (["01 - Daily Notes/%s.md" % name] if bad else [])


def test_a_note_with_type_daily_is_checked_wherever_it_is(env):
    index(env, "A")
    note(env, "A.md")
    note(env, "elsewhere/today.md", "---\ntype: daily\n---\n")
    assert only(findings(env), "bad_daily_name") == ["elsewhere/today.md"]


# ---------- output, exit codes, work items ----------
def test_output_has_one_line_per_finding_in_kind_order_with_a_summary(env, capsys):
    index(env, "A")
    note(env, "A.md", OK + "[[x]] [[y]]\n")
    note(env, "Lonely.md", "no frontmatter")
    assert vc.main([]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "broken_link: A.md -> [[x]]" and lines[1] == "broken_link: A.md -> [[y]]"
    assert "orphan: Lonely.md" in lines and "missing_from_index: Lonely.md" in lines and "missing_status: Lonely.md (no frontmatter)" in lines
    assert lines[-1] == "summary: 3 notes, 5 finding(s)"


def test_the_check_never_changes_a_note(env):
    index(env, "A")
    note(env, "A.md", "no frontmatter [[broken]]\n")
    before = {p: p.read_bytes() for p in (env / "vault").rglob("*") if p.is_file()}
    vc.main(["--raise"])
    assert {p: p.read_bytes() for p in before} == before


def test_raise_adds_one_item_per_kind_not_per_finding(env):
    index(env, "A")
    note(env, "A.md", OK + "[[x]] [[y]] [[z]]\n")
    vc.main(["--raise"])
    rows = [r for r in intent_store.read_jsonl("intents.jsonl", str(env / "logs")) if r["kind"].startswith("vault_")]
    assert [r["kind"] for r in rows] == ["vault_broken_link"]
    assert rows[0]["tier"] == 1 and rows[0]["value"] == 3 and rows[0]["shadow"] is False and rows[0]["what"].startswith("vault check: 3 x broken link (first: A.md -> [[x]])")


def test_raise_twice_does_not_pile_up_items_even_when_the_findings_change(env):
    index(env, "A")
    note(env, "A.md", OK + "[[x]]\n")
    vc.main(["--raise"])
    note(env, "A.md", OK + "[[aaa]] [[x]] [[y]]\n")                       # the first finding line is now a different one
    vc.main(["--raise"])
    assert len([r for r in intent_store.read_jsonl("intents.jsonl", str(env / "logs")) if r["kind"] == "vault_broken_link"]) == 1


def test_raise_keeps_other_items_in_the_work_list(env):
    intent_store.write_intents([intent_store.new_intent("homework", "other", tier=1, value=3, dedupe="o")], str(env / "logs"))
    index(env, "A")
    note(env, "A.md", OK + "[[x]]\n")
    vc.main(["--raise"])
    assert sorted(r["kind"] for r in intent_store.read_jsonl("intents.jsonl", str(env / "logs"))) == ["homework", "vault_broken_link"]


def test_a_clean_vault_raises_nothing_and_writes_no_log_files(env):
    index(env, "A")
    note(env, "A.md")
    vc.main(["--raise"])
    assert not (env / "logs").exists() or os.listdir(env / "logs") == []


def test_job_mode_always_exits_0_when_the_vault_exists(env):
    index(env, "A")
    note(env, "A.md", OK + "[[x]]\n")
    assert vc.main(["--raise"]) == 1 and vc.main(["--raise", "--job"]) == 0


def test_the_script_runs_as_a_program_and_its_source_compiles_for_python_3_10(env):
    index(env, "A")
    note(env, "A.md")
    e = dict(os.environ, HARNESS_ROOT=str(env), HARNESS_VAULT=str(env / "vault"), HARNESS_LOGS=str(env / "logs"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, os.path.join(HERE, "vault_check.py")], capture_output=True, text=True, env=e)
    assert r.returncode == 0 and "0 finding(s)" in r.stdout
    import ast
    ast.parse(open(os.path.join(HERE, "vault_check.py"), encoding="utf-8").read(), feature_version=(3, 10))
