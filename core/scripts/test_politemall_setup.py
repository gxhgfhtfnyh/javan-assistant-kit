# -*- coding: utf-8 -*-
"""Tests for politemall_setup.py's pure helpers. The interactive main() (real input()/getpass()/
keyring/subprocess calls) is out of scope for a unit test by design -- build_config() and
save_config() are the testable core; never any password/secret passes through either."""
import json
from pathlib import Path

import politemall_setup as ps


class TestBuildConfig:
    def test_builds_the_expected_shape(self):
        data = ps.build_config("someone@myrp.edu.sg", r"D:\MyVault\School")
        assert data["username"] == "someone@myrp.edu.sg"
        assert data["vault_school_root"] == r"D:\MyVault\School"
        assert "politemall-someone" in data["profile_dir"]

    def test_no_vault_root_given_stores_none_not_an_empty_string(self):
        data = ps.build_config("someone@myrp.edu.sg", "")
        assert data["vault_school_root"] is None

    def test_an_explicit_profile_dir_overrides_the_derived_one(self):
        data = ps.build_config("someone@myrp.edu.sg", None, profile_dir=r"E:\custom")
        assert data["profile_dir"] == r"E:\custom"

    def test_no_secret_fields_exist_in_the_output(self):
        data = ps.build_config("someone@myrp.edu.sg", r"D:\x")
        blob = json.dumps(data).lower()
        assert "password" not in blob and "secret" not in blob and "totp" not in blob


class TestSaveConfig:
    def test_writes_valid_json_to_the_given_path(self, tmp_path):
        target = tmp_path / "sub" / "config.json"
        data = ps.build_config("someone@myrp.edu.sg", r"D:\x")
        out = ps.save_config(data, target)
        assert out == target
        assert json.loads(target.read_text(encoding="utf-8")) == data

    def test_creates_missing_parent_directories(self, tmp_path):
        target = tmp_path / "does" / "not" / "exist" / "config.json"
        ps.save_config({"username": "x"}, target)
        assert target.exists()


class TestFindLooseModuleItems:
    """A loaded vault may have notes sitting at the vault root, not yet inside a School folder.
    Never a deep/unbounded scan: only the vault's top level, and only names matching a known
    module code, so this can't accidentally sweep up unrelated personal files."""

    def test_finds_a_loose_module_folder_at_the_vault_root(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "A210 Protein Technologies").mkdir(parents=True)
        school = vault / "School"
        moves = ps.find_loose_module_items(vault, school)
        assert moves == [(vault / "A210 Protein Technologies", school / "A210 Protein Technologies")]

    def test_ignores_items_already_inside_the_school_folder(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "School" / "A210 Protein Technologies").mkdir(parents=True)
        assert ps.find_loose_module_items(vault, vault / "School") == []

    def test_ignores_unrelated_files_and_dotfiles(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Personal Journal").mkdir()
        (vault / ".obsidian").mkdir()
        assert ps.find_loose_module_items(vault, vault / "School") == []

    def test_missing_vault_root_returns_empty_not_a_crash(self, tmp_path):
        assert ps.find_loose_module_items(tmp_path / "nope", tmp_path / "nope" / "School") == []

    def test_matches_a_module_code_inside_a_longer_filename(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "A360 Bioinformatics Notes.md").parent.mkdir(parents=True, exist_ok=True)
        (vault / "A360 Bioinformatics Notes.md").write_text("x", encoding="utf-8")
        moves = ps.find_loose_module_items(vault, vault / "School")
        assert len(moves) == 1 and moves[0][0].name == "A360 Bioinformatics Notes.md"


class TestPreviewMoves:
    def test_empty_list_says_nothing_found(self):
        assert "Nothing loose found" in ps.preview_moves([])

    def test_lists_every_planned_move(self):
        out = ps.preview_moves([(Path("a/A210"), Path("a/School/A210"))])
        assert "A210" in out and "->" in out


class TestApplyMoves:
    def test_moves_a_real_folder(self, tmp_path):
        src = tmp_path / "A210 Notes"
        src.mkdir()
        (src / "note.md").write_text("hi", encoding="utf-8")
        dest = tmp_path / "School" / "A210 Notes"
        done, skipped = ps.apply_moves([(src, dest)])
        assert done == [(src, dest)]
        assert skipped == []
        assert (dest / "note.md").read_text(encoding="utf-8") == "hi"
        assert not src.exists()

    def test_never_overwrites_an_existing_destination(self, tmp_path):
        src = tmp_path / "A210 Notes"
        src.mkdir()
        dest = tmp_path / "School" / "A210 Notes"
        dest.mkdir(parents=True)
        (dest / "already_here.md").write_text("keep me", encoding="utf-8")
        done, skipped = ps.apply_moves([(src, dest)])
        assert done == []
        assert len(skipped) == 1 and "already exists" in skipped[0][2]
        assert (dest / "already_here.md").read_text(encoding="utf-8") == "keep me"  # untouched
        assert src.exists()  # the source was never touched either


class TestPromptHelpers:
    def test_prompt_username_blank_input_keeps_existing(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert ps.prompt_username("old@myrp.edu.sg") == "old@myrp.edu.sg"

    def test_prompt_username_real_input_overrides_existing(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "new@myrp.edu.sg")
        assert ps.prompt_username("old@myrp.edu.sg") == "new@myrp.edu.sg"

    def test_prompt_vault_root_blank_keeps_existing(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "  ")
        assert ps.prompt_vault_root(r"D:\old") == r"D:\old"
