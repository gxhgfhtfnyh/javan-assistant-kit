#!/usr/bin/env python3
"""Tests for generate.py"""

import json, sys
from datetime import date
from pathlib import Path
import pytest

from generate import (
    load_questions, flat_questions, resolve_answers, render, is_safe_path, generate, main,
    MissingRequired, BadChoice, UnknownPlaceholder, UnsafePath,
)


def make_fixture_kit(tmp_path: Path) -> Path:
    kit = tmp_path / "kit"
    kit.mkdir()
    tpl = kit / "templates"
    (tpl / "vault" / "Notes").mkdir(parents=True)
    (kit / "questions.json").write_text(json.dumps({
        "rounds": [{"id": "r1", "questions": [
            {"id": "name", "text": "Your name", "required": True},
            {"id": "role", "text": "Your role", "default": "developer"},
            {"id": "lang", "text": "Favorite language", "choices": ["python", "rust", "go"]},
            {"id": "color", "text": "Favorite color", "default": "blue"},
        ]}],
        "derive": {
            "lang_full": {"from": "lang", "map": {"python": "Python", "rust": "Rust", "go": "Go"}},
            "greeting": {"from": "role", "map": {"developer": "Hello dev", "manager": "Hello mgr"}}
        }
    }), encoding="utf-8")
    (kit / "structure.json").write_text(json.dumps({"folders": ["{{name}}/docs", "logs"]}), encoding="utf-8")
    (tpl / "CLAUDE.md.tpl").write_text("# {{name}}\n\nRole: {{role}}\nLang: {{lang_full}}\n", encoding="utf-8")
    (tpl / "vault" / "Index.md.tpl").write_text("## Index\n\n{{answers_table}}\n", encoding="utf-8")
    (tpl / "vault" / "Notes" / "About Me.md.tpl").write_text("About {{name}}\n\n{{greeting}}\n", encoding="utf-8")
    return kit


def test_defaults_taken_for_ok_empty_missing():
    q = load_questions(Path(__file__).resolve().parent.parent / "questions.json")
    a = {"name": "Tester", "about": "", "goals": "ok", "os": "DEFAULT", "tools": "  ok  "}
    v, s = resolve_answers(q, a, today=date(2025, 1, 15))
    assert v["about"] == "not told yet" and s["about"] == "default"
    assert v["goals"] == "keeping track of deadlines and notes" and s["goals"] == "default"
    assert v["os"] == "Windows" and s["os"] == "default"
    assert v["tools"] == "none yet" and s["tools"] == "default"


def test_required_missing_raises():
    q = load_questions(Path(__file__).resolve().parent.parent / "questions.json")
    with pytest.raises(MissingRequired) as e:
        resolve_answers(q, {"about": "test"})
    assert e.value.question_id == "name"


def test_bad_choice_raises():
    q = load_questions(Path(__file__).resolve().parent.parent / "questions.json")
    with pytest.raises(BadChoice) as e:
        resolve_answers(q, {"name": "Test", "voice": "invalid"})
    assert e.value.question_id == "voice"
    assert set(e.value.allowed) == {"friend", "professional", "direct"}


def test_derive_works_and_unknown_source_raises():
    q = {"rounds": [{"id": "r1", "questions": [
        {"id": "lang", "text": "Language", "choices": ["python", "rust"]},
        {"id": "role", "text": "Role", "default": "dev"},
    ]}], "derive": {
        "lang_full": {"from": "lang", "map": {"python": "Python", "rust": "Rust"}},
        "greeting": {"from": "role", "map": {"dev": "Hello dev", "mgr": "Hello mgr"}}
    }}
    v, _ = resolve_answers(q, {"lang": "python", "role": "dev"})
    assert v["lang_full"] == "Python" and v["greeting"] == "Hello dev"
    with pytest.raises(BadChoice) as e:
        resolve_answers(q, {"lang": "javascript", "role": "dev"})
    assert e.value.question_id == "lang"


def test_answers_table_marks_default_and_skipped():
    q = {"rounds": [{"id": "r1", "questions": [
        {"id": "a", "text": "Q A", "default": "defA"},
        {"id": "b", "text": "Q B", "required": True},
        {"id": "c", "text": "Q C", "default": "defC"},
    ]}], "derive": {}}
    v, s = resolve_answers(q, {"b": "givenB"}, today=date(2025, 1, 15))
    assert s["a"] == "default" and s["b"] == "given" and s["c"] == "default"
    t = v["answers_table"]
    assert "- **Q A**: defA (default)" in t
    assert "- **Q B**: givenB" in t
    assert "- **Q C**: defC (default)" in t


def test_render_replaces_keys_and_raises_on_unknown():
    v = {"name": "Alice", "role": "dev"}
    assert render("Hello {{name}}, you are a {{role}}", v) == "Hello Alice, you are a dev"
    with pytest.raises(UnknownPlaceholder) as e:
        render("Hello {{unknown}}", v)
    assert e.value.key == "unknown"


def test_generate_creates_all_files_and_folders(tmp_path):
    kit = make_fixture_kit(tmp_path)
    out = tmp_path / "out"
    r = generate(kit, {"name": "TestUser", "lang": "python", "role": "developer"}, out, today=date(2025, 1, 15))
    assert "TestUser/docs" in r["folders"] and "logs" in r["folders"]
    c = set(r["created"])
    assert {"CLAUDE.md", "vault/Index.md", "vault/Notes/About Me.md"} <= c
    for f in c:
        txt = (out / f).read_text(encoding="utf-8")
        assert "{{" not in txt and "TestUser" in txt


def test_generate_twice_reports_skipped_no_changes(tmp_path):
    kit = make_fixture_kit(tmp_path)
    out = tmp_path / "out"
    a = {"name": "TestUser", "lang": "python", "role": "developer"}
    r1 = generate(kit, a, out, today=date(2025, 1, 15))
    r2 = generate(kit, a, out, today=date(2025, 1, 15))
    assert len(r1["created"]) == 3 and r1["skipped"] == [] and r1["overwritten"] == []
    assert r2["created"] == [] and len(r2["skipped"]) == 3 and r2["overwritten"] == []
    assert (out / "CLAUDE.md").stat().st_mtime == (out / "CLAUDE.md").stat().st_mtime


def test_changed_target_skipped_without_force_overwritten_with_force(tmp_path):
    kit = make_fixture_kit(tmp_path)
    out = tmp_path / "out"
    a = {"name": "TestUser", "lang": "python", "role": "developer"}
    generate(kit, a, out, today=date(2025, 1, 15))
    (out / "CLAUDE.md").write_text("MODIFIED", encoding="utf-8")
    r = generate(kit, a, out, force=False, today=date(2025, 1, 15))
    assert "CLAUDE.md" in r["skipped"] and (out / "CLAUDE.md").read_text() == "MODIFIED"
    r2 = generate(kit, a, out, force=True, today=date(2025, 1, 15))
    assert "CLAUDE.md" in r2["overwritten"] and "TestUser" in (out / "CLAUDE.md").read_text()


def test_unsafe_path_raises(tmp_path):
    kit = make_fixture_kit(tmp_path)
    (kit / "templates" / "bad{{name}}.tpl").write_text("x", encoding="utf-8")
    with pytest.raises(UnsafePath):
        generate(kit, {"name": "..", "lang": "python", "role": "developer"}, tmp_path / "out", today=date(2025, 1, 15))


def test_main_returns_0_with_json_report(tmp_path, capsys):
    kit = make_fixture_kit(tmp_path)
    out = tmp_path / "out"
    af = tmp_path / "answers.json"
    af.write_text(json.dumps({"name": "TestUser", "lang": "python", "role": "developer"}), encoding="utf-8")
    sys.argv = ["g", "--answers", str(af), "--out", str(out), "--kit", str(kit)]
    assert main() == 0
    rep = json.loads(capsys.readouterr().out)
    assert "created" in rep and len(rep["created"]) == 3


def test_main_returns_2_with_error_for_missing_required(tmp_path, capsys):
    kit = make_fixture_kit(tmp_path)
    out = tmp_path / "out"
    af = tmp_path / "answers.json"
    af.write_text(json.dumps({"lang": "python", "role": "developer"}), encoding="utf-8")
    sys.argv = ["g", "--answers", str(af), "--out", str(out), "--kit", str(kit)]
    assert main() == 2
    err = capsys.readouterr().err
    assert "error:" in err and "required question 'name'" in err


def test_flat_questions_order():
    q = {"rounds": [{"id": "r1", "questions": [{"id": "a"}, {"id": "b"}]}, {"id": "r2", "questions": [{"id": "c"}]}]}
    assert [x["id"] for x in flat_questions(q)] == ["a", "b", "c"]


def test_unknown_ids_in_answers_ignored():
    q = {"rounds": [{"id": "r1", "questions": [{"id": "a", "text": "A", "default": "def"}]}], "derive": {}}
    v, s = resolve_answers(q, {"a": "given", "unknown": "ignored"})
    assert v["a"] == "given" and s["a"] == "given" and "unknown" not in v


def test_is_safe_path():
    assert is_safe_path("folder/file.txt") and is_safe_path("a/b/c")
    assert not is_safe_path("/absolute") and not is_safe_path("folder/..")
    for bad in r"a\b a:b a*b a?b a\"b a<b a>b a|b".split():
        assert not is_safe_path(bad)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])