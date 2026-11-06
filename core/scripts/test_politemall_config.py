# -*- coding: utf-8 -*-
"""Tests for politemall_config.py: generalized so a different student at the same school can run
the politemall scripts from a config file instead of hardcoded values. Never reads/writes a
password or TOTP secret -- those stay in Credential Manager exactly as before."""
import json

import politemall_config as cfg


class TestResolveProfileDir:
    def test_explicit_override_wins(self):
        assert cfg.resolve_profile_dir("anyone@school.edu", override=r"C:\custom\path") == r"C:\custom\path"

    def test_derives_a_safe_folder_name_from_the_username(self):
        p = cfg.resolve_profile_dir("12345678@myrp.edu.sg")
        assert "politemall-12345678" in p
        assert "@" not in p  # the @school.edu part must not leak into a folder name

    def test_falls_back_to_default_when_no_username_at_all(self):
        p = cfg.resolve_profile_dir(None)
        assert "politemall-default" in p


class TestLoad:
    def test_missing_file_returns_defaults_with_a_derived_profile_dir(self, tmp_path):
        data = cfg.load(tmp_path / "nope.json")
        assert data["username"] is None
        assert "politemall-default" in data["profile_dir"]

    def test_real_config_file_is_read_and_merged_over_defaults(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"username": "someone@myrp.edu.sg", "vault_school_root": r"D:\vault\School"}), encoding="utf-8")
        data = cfg.load(p)
        assert data["username"] == "someone@myrp.edu.sg"
        assert data["vault_school_root"] == r"D:\vault\School"
        assert data["home_url"] == cfg.DEFAULTS["home_url"]  # untouched keys keep their default

    def test_an_explicit_profile_dir_in_the_file_is_not_overwritten(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"username": "someone@myrp.edu.sg", "profile_dir": r"E:\custom"}), encoding="utf-8")
        assert cfg.load(p)["profile_dir"] == r"E:\custom"

    def test_malformed_json_falls_back_to_defaults_not_a_crash(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        data = cfg.load(p)
        assert data["username"] is None

    def test_unknown_keys_in_the_file_are_ignored_not_injected(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"username": "x@y.edu", "some_future_field": "z"}), encoding="utf-8")
        assert "some_future_field" not in cfg.load(p)

    def test_a_non_dict_json_body_falls_back_to_defaults(self, tmp_path):
        p = tmp_path / "list.json"
        p.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        assert cfg.load(p)["username"] is None
