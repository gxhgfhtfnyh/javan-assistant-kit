# -*- coding: utf-8 -*-
"""Tests for backlink_suggester.py: which titles count, which text is searched, how a note is found, the output and the real process."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import backlink_suggester as b  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    v = root / "vault"
    for d in ("05 - Resources", "02 - Projects/Website", "04 - Archive", "01 - Daily Notes", ".obsidian"):
        (v / d).mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_VAULT", str(v))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    for rel in ("05 - Resources/Lessons Pack.md", "05 - Resources/Deadlines.md", "05 - Resources/Autonomy Tiers.md", "Active Priorities.md",
                "02 - Projects/Website/Website Plan.md", "04 - Archive/Old Idea.md", "01 - Daily Notes/2026-10-31.md",
                "01 - Daily Notes/Daily Note Template.md", ".obsidian/Hidden Thing.md", "_scratch.md", "Art.md", "Café Menu.md",
                "05 - Resources/Free Helper Models (optional).md"):
        (v / rel).write_text("# note\n", encoding="utf-8")
    return SimpleNamespace(root=root, vault=v)


def note(env, name, text):
    p = env.vault / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ---------- the vault's titles ----------
def test_titles_skip_archive_template_hidden_daily_underscore_and_short(env):
    titles = b.all_note_titles()
    assert "Lessons Pack" in titles and "Website Plan" in titles and "Café Menu" in titles
    for hidden in ("Old Idea", "2026-10-31", "Daily Note Template", "Hidden Thing", "_scratch", "Art"):
        assert hidden not in titles, hidden


def test_titles_exclude_the_note_itself_and_the_first_of_two_same_names_wins(env):
    me = note(env, "Active Priorities.md", "x")
    assert "Active Priorities" not in b.all_note_titles(exclude_path=me)
    note(env, "02 - Projects/Website/Deadlines.md", "second")
    assert b.all_note_titles()["Deadlines"] == env.vault / "02 - Projects" / "Website" / "Deadlines.md"      # sorted walk: 02 comes before 05


# ---------- the text ----------
def test_visible_text_drops_frontmatter_code_links_and_urls():
    text = ("---\ntags: Lessons Pack\n---\nreal Deadlines text `Autonomy Tiers` and [[Lessons Pack]] and [Active Priorities](x.md) "
            "see https://example.test/Website Plan\n```\nWebsite Plan in a fence\n```\nend")
    out = b.visible_text(text)
    assert "Deadlines" in out and "end" in out
    for gone in ("Autonomy Tiers", "Lessons Pack", "Active Priorities", "Website Plan", "tags"):
        assert gone not in out, gone


def test_linked_titles_understand_alias_heading_folder_extension_and_case():
    text = "[[Lessons Pack|the pack]] [[Deadlines#Soon]] [[05 - Resources/Autonomy Tiers]] [[Active Priorities.md]] ![[Pic]] [[Table\\|x]]"
    assert b.linked_titles(text) == {"lessons pack", "deadlines", "autonomy tiers", "active priorities", "pic", "table"}


def test_find_suggestions_matches_whole_words_with_the_same_capitalisation():
    titles = {"Deadlines": 1, "Lessons Pack": 1, "Art": 1, "Autonomy Tiers": 1}
    text = "Check Deadlines and the Lessons Pack. Not Deadlinesx, not lessons pack, not Autonomy-Tiers."
    assert b.find_suggestions(text, titles) == ["Deadlines", "Lessons Pack"]
    assert b.find_suggestions(text, titles, ignore_case=True) == ["Deadlines", "Lessons Pack"]
    assert b.find_suggestions("the lessons pack", titles) == [] and b.find_suggestions("the lessons pack", titles, ignore_case=True) == ["Lessons Pack"]


def test_a_title_already_linked_in_any_form_is_not_suggested():
    titles = {"Deadlines": 1, "Lessons Pack": 1}
    assert b.find_suggestions("Deadlines [[Deadlines|due dates]] and Lessons Pack [[Lessons Pack#Top]]", titles) == []
    assert b.find_suggestions("Deadlines and [[Lessons Pack]]", titles) == ["Deadlines"]


def test_titles_with_punctuation_and_accents_are_matched_literally():
    titles = {"Free Helper Models (optional)": 1, "Café Menu": 1, "C++ Notes": 1}
    text = "See Free Helper Models (optional) and the Café Menu, plus C++ Notes."
    assert b.find_suggestions(text, titles) == ["C++ Notes", "Café Menu", "Free Helper Models (optional)"]
    assert b.find_suggestions("Café Menus", titles) == []


# ---------- finding the note ----------
def test_end_to_end_with_a_name_a_vault_path_or_a_full_path(env):
    p = note(env, "02 - Projects/Website/Website Notes.md", "Due soon: see Deadlines and the Lessons Pack. Also [[Autonomy Tiers]] and Website Plan.")
    for arg in (str(p), "02 - Projects/Website/Website Notes.md", "Website Notes", "Website Notes.md"):
        assert b.suggest_backlinks(arg) == ["Deadlines", "Lessons Pack", "Website Plan"], arg


def test_a_note_never_suggests_itself(env):
    p = note(env, "Active Priorities.md", "Active Priorities lists the Deadlines")
    assert b.suggest_backlinks(p) == ["Deadlines"]


def test_a_note_outside_the_vault_can_be_checked(env, tmp_path):
    outside = tmp_path / "scratch.md"
    outside.write_text("remember the Deadlines", encoding="utf-8")
    assert b.suggest_backlinks(outside) == ["Deadlines"]


def test_missing_note_raises_and_main_exits_2(env, capsys):
    with pytest.raises(FileNotFoundError):
        b.suggest_backlinks("No Such Note")
    assert b.main(["No Such Note"]) == 2
    assert "cannot read note" in capsys.readouterr().out


def test_a_directory_is_not_a_note(env, capsys):
    assert b.main([str(env.vault / "05 - Resources")]) == 2


def test_the_note_is_never_modified(env):
    p = note(env, "Reader.md", "Deadlines and Lessons Pack")
    before = (p.read_bytes(), p.stat().st_mtime_ns)
    b.suggest_backlinks(p)
    assert (p.read_bytes(), p.stat().st_mtime_ns) == before


def test_invalid_utf8_and_huge_notes_do_not_crash(env, monkeypatch):
    p = env.vault / "Binary.md"
    p.write_bytes(b"Deadlines \xff\xfe\x00 end")
    assert b.suggest_backlinks(p) == ["Deadlines"]
    monkeypatch.setattr(b, "MAX_NOTE_BYTES", 20)
    big = note(env, "Big.md", "Deadlines " + "x" * 100 + " Lessons Pack")
    assert b.suggest_backlinks(big) == ["Deadlines"]


# ---------- the command line ----------
def test_main_prints_a_list_or_the_all_clear(env, capsys):
    note(env, "Reader.md", "Deadlines and Lessons Pack")
    assert b.main(["Reader"]) == 0
    out = capsys.readouterr().out
    assert "2 possible missing link(s):" in out and "  [[Deadlines]]" in out and "  [[Lessons Pack]]" in out
    note(env, "Quiet.md", "nothing to link")
    assert b.main(["Quiet"]) == 0 and "No missing links found." in capsys.readouterr().out


def test_main_json_and_ignore_case(env, capsys):
    note(env, "Reader.md", "the lessons pack")
    assert b.main(["Reader", "--json", "--ignore-case"]) == 0
    assert json.loads(capsys.readouterr().out)["suggestions"] == ["Lessons Pack"]


def test_main_without_a_note_is_a_usage_error(capsys):
    assert b.main([]) == 1


def test_real_process_with_its_own_environment(tmp_path):
    root = tmp_path / "r"
    (root / "vault").mkdir(parents=True)
    (root / "vault" / "Café Menu.md").write_text("x", encoding="utf-8")
    (root / "vault" / "Reader.md").write_text("Try the Café Menu today", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, "-B", str(HERE / "backlink_suggester.py"), "Reader"], capture_output=True, env=env, timeout=30)
    assert r.returncode == 0 and "[[Café Menu]]".encode("utf-8") in r.stdout
