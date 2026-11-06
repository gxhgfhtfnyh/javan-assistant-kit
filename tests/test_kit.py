# -*- coding: utf-8 -*-
"""Kit-level checks on the REAL questions, templates and personas (the generator's own unit tests live in test_generate.py).
The acceptance idea: three different fake people go through the interview answers, and what comes out is complete, personal, free of leftovers and free of anything from the maker's PC."""
import json
import os
import re
import sys

import pytest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)
import generate as G  # noqa: E402

PERSONAS = sorted(f for f in os.listdir(os.path.join(KIT, "personas")) if f.endswith(".json"))
FORBIDDEN = ["discord.com/api", "Bearer ", "C:\\Users"]  # generic leak markers; personal names live in the builder's private denylist (env KIT_DENYLIST), never in the kit
KEY_LIKE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}|\b[A-Za-z0-9]{32,}\b")
EXPECTED = ["CLAUDE.md", "vault/VAULT-INDEX.md", "vault/Active Priorities.md", "vault/Interview Log.md", "vault/01 - Daily Notes/Daily Note Template.md", "vault/03 - Personal/About Me.md",
            "vault/05 - Resources/Lessons Pack.md", "vault/05 - Resources/How This Assistant Works.md"]


def load(name):
    return json.load(open(os.path.join(KIT, "personas", name), encoding="utf-8"))


def read(path):
    return open(path, encoding="utf-8").read()


def all_kit_text_files():
    for root, dirs, files in os.walk(KIT):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".pytest_cache", "out", "tests")]
        for f in files:
            if f.endswith((".tpl", ".json", ".md", ".py")) and f != "generate.py" and not f.startswith("test_"):
                yield os.path.join(root, f)


def test_the_interview_has_at_most_fifteen_questions_with_unique_ids_and_sane_defaults():
    q = G.load_questions(os.path.join(KIT, "questions.json"))
    flat = G.flat_questions(q)
    ids = [x["id"] for x in flat]
    assert len(ids) == len(set(ids)) and 12 <= len(ids) <= 15
    for x in flat:
        assert x["text"].strip()
        if x.get("choices") and "default" in x:
            assert x["default"] in x["choices"], x["id"]
    assert [x["id"] for x in flat if x.get("required")] == ["name"]


def test_every_placeholder_in_every_template_can_be_filled():
    q = G.load_questions(os.path.join(KIT, "questions.json"))
    known = {x["id"] for x in G.flat_questions(q)} | set(q["derive"]) | {"date", "answers_table"} | set(G.python_values())
    seen = set()
    for root, _d, files in os.walk(os.path.join(KIT, "templates")):
        for f in files:
            seen |= set(re.findall(r"\{\{([A-Za-z0-9_]+)\}\}", read(os.path.join(root, f)) + os.path.join(root, f)))
    assert seen and seen <= known, seen - known


def test_nothing_from_the_makers_pc_is_in_the_kit():
    hits = []
    for path in all_kit_text_files():
        text = read(path)
        for bad in FORBIDDEN:
            if bad in text:
                hits.append((os.path.relpath(path, KIT), bad))
        if KEY_LIKE.search(text):
            hits.append((os.path.relpath(path, KIT), "key-like string"))
    assert hits == []


@pytest.mark.parametrize("persona", PERSONAS)
def test_each_persona_gets_a_complete_personal_folder(persona, tmp_path):
    answers = load(persona)
    out = tmp_path / "assistant"
    report = G.generate(KIT, answers, str(out), today="2026-11-01")
    for rel in EXPECTED:
        assert (out / rel).is_file(), rel
    for folder in ("vault/00 - Inbox", "vault/02 - Projects", "vault/04 - Archive", "logs"):
        assert (out / folder).is_dir(), folder
    assert report["created"] and not report["overwritten"]
    copied_code = ("scripts/", ".claude/skills/", ".claude/agents/")        # copied byte for byte from core/: they may legitimately mention braces or a webhook address
    for path in out.rglob("*"):
        if path.is_file() and not path.relative_to(out).as_posix().startswith(copied_code):
            text = path.read_text(encoding="utf-8")
            assert "{{" not in text and "}}" not in text, path.name
            for bad in FORBIDDEN:
                assert bad not in text, (path.name, bad)
    boot = (out / "CLAUDE.md").read_text(encoding="utf-8")
    assert answers["name"] in boot and "2026-11-01" in boot
    assert "Evidence only" in boot and "approval of that exact content" in boot       # the rules that never lapse arrived
    assert "never" in boot.lower() and "Quiet hours" in boot and "Usage and cost" in boot


def test_the_voice_choice_changes_the_boot_file(tmp_path):
    q = G.load_questions(os.path.join(KIT, "questions.json"))
    friend = G.generate(KIT, load("student.json"), str(tmp_path / "a"), today="2026-11-01")
    pro = G.generate(KIT, load("freelancer.json"), str(tmp_path / "b"), today="2026-11-01")
    assert friend["created"] and pro["created"]
    a = (tmp_path / "a" / "CLAUDE.md").read_text(encoding="utf-8")
    b = (tmp_path / "b" / "CLAUDE.md").read_text(encoding="utf-8")
    assert q["derive"]["voice_rules"]["map"]["friend"] in a and q["derive"]["voice_rules"]["map"]["professional"] in b and a != b


def test_a_person_who_only_gives_a_name_still_gets_a_working_setup_with_defaults_marked(tmp_path):
    out = tmp_path / "m"
    G.generate(KIT, load("minimal.json"), str(out), today="2026-11-01")
    log = (out / "vault" / "Interview Log.md").read_text(encoding="utf-8")
    assert "Sam" in log and "(default)" in log
    boot = (out / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Jarvis" in boot and "chill friend" in boot and "midnight to 9am" in boot          # the default name, voice and quiet hours


def test_building_twice_changes_nothing_and_deletes_nothing(tmp_path):
    out = tmp_path / "again"
    G.generate(KIT, load("student.json"), str(out), today="2026-11-01")
    extra = out / "vault" / "02 - Projects" / "my own note.md"
    extra.write_text("keep me", encoding="utf-8")
    before = {str(p): p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file()}
    second = G.generate(KIT, load("student.json"), str(out), today="2026-11-01")
    after = {str(p): p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file()}
    assert before == after and not second["created"] and extra.read_text(encoding="utf-8") == "keep me"


def test_a_capitalised_choice_still_matches_the_voice_table_and_a_non_object_answers_file_is_refused(tmp_path, capsys):
    out = tmp_path / "cap"
    G.generate(KIT, {"name": "Ana", "voice": "Friend", "os": "WINDOWS"}, str(out), today="2026-11-01")
    assert "chill friend" in (out / "CLAUDE.md").read_text(encoding="utf-8")
    bad = tmp_path / "list.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    assert G.main(["--answers", str(bad), "--out", str(tmp_path / "x"), "--kit", KIT]) == 2
    assert "JSON object" in capsys.readouterr().err
    assert not (tmp_path / "x").exists()


def test_the_cli_builds_a_persona_end_to_end(tmp_path, capsys):
    out = tmp_path / "cli"
    assert G.main(["--answers", os.path.join(KIT, "personas", "student.json"), "--out", str(out), "--kit", KIT]) == 0
    report = json.loads(capsys.readouterr().out)
    assert "CLAUDE.md" in report["created"] and (out / "vault" / "VAULT-INDEX.md").is_file()


def test_force_on_an_identical_file_reports_it_skipped_not_overwritten(tmp_path):
    out = tmp_path / "same"
    G.generate(KIT, load("student.json"), str(out), today="2026-11-01")
    again = G.generate(KIT, load("student.json"), str(out), force=True, today="2026-11-01")
    assert not again["overwritten"] and not again["created"] and "CLAUDE.md" in again["skipped"]


def test_a_wrong_choice_is_refused_even_when_no_derive_table_uses_it():
    with pytest.raises(G.BadChoice) as e:
        G.resolve_answers(G.load_questions(os.path.join(KIT, "questions.json")), {"name": "Ana", "os": "Amiga"})
    assert e.value.question_id == "os"


def test_a_derive_table_refuses_a_source_value_it_does_not_know():
    q = {"rounds": [{"questions": [{"id": "mood", "text": "Mood?", "default": "calm"}]}], "derive": {"mood_rules": {"from": "mood", "map": {"calm": "slow down"}}}}
    with pytest.raises(G.BadChoice) as e:
        G.resolve_answers(q, {"mood": "furious"})
    assert e.value.question_id == "mood"
    values, _ = G.resolve_answers(q, {})
    assert values["mood_rules"] == "slow down"


def test_the_vault_comes_configured_for_obsidian_with_daily_notes_pointing_at_the_template(tmp_path):
    out = tmp_path / "assistant"
    G.generate(KIT, load("student.json"), str(out), today="2026-11-01")
    cfg = out / "vault" / ".obsidian"
    daily = json.loads((cfg / "daily-notes.json").read_text(encoding="utf-8"))
    assert daily["folder"] == "01 - Daily Notes" and daily["format"] == "YYYY-MM-DD"
    assert (out / "vault" / (daily["template"] + ".md")).is_file()
    app = json.loads((cfg / "app.json").read_text(encoding="utf-8"))
    assert app["newFileFolderPath"] == "00 - Inbox" and (out / "vault" / app["newFileFolderPath"]).is_dir()
    assert "daily-notes" in json.loads((cfg / "core-plugins.json").read_text(encoding="utf-8"))
    assert json.loads((cfg / "appearance.json").read_text(encoding="utf-8"))


def test_the_autopilot_comes_switched_off_with_a_plain_explanation_of_how_to_switch_it_on(tmp_path):
    out = tmp_path / "assistant"
    G.generate(KIT, load("student.json"), str(out), today="2026-11-01")
    text = (out / "logs" / "autopilot_off.txt").read_text(encoding="utf-8")
    assert "Delete this file" in text and "uses your Claude plan" in text and "{{" not in text
