# -*- coding: utf-8 -*-
"""Tests added in review (2026-10-31) for ransomware_canary.py: it must go through the shared work-list store (the first version carried its own copy that rewrote the whole file)."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intent_store  # noqa: E402
import ransomware_canary as rc  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    for d in ("vault/00 - Inbox", "logs"):
        (tmp_path / d).mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    return tmp_path


def test_an_alert_keeps_every_other_intent_already_in_the_work_list(env):
    other = intent_store.new_intent("homework", "an unrelated item", tier=1, value=3, dedupe="other")
    intent_store.write_intents([other], str(env / "logs"))
    rc.check()
    (env / "vault" / "00 - Inbox" / "~canary_01.txt").write_text("encrypted!", encoding="utf-8")
    result = rc.check()
    assert result["alerts_raised"] == 1
    kinds = sorted(i["kind"] for i in intent_store.read_jsonl("intents.jsonl", str(env / "logs")))
    assert kinds == ["homework", "ransomware_suspected"]


def test_the_alert_is_an_ask_first_value_5_item_and_never_shadow(env):
    rc.check()
    os.remove(env / "vault" / "00 - Inbox" / "~canary_02.txt")
    rc.check()
    row = [i for i in intent_store.read_jsonl("intents.jsonl", str(env / "logs")) if i["kind"] == "ransomware_suspected"][0]
    assert row["tier"] == 2 and row["value"] == 5 and row["shadow"] is False and "missing" in row["what"]


def test_the_decoys_are_plain_text_files_so_the_note_search_never_indexes_them(env):
    rc.check()
    names = [p.name for p in rc._get_canary_paths(env / "vault", env / "logs")]
    assert names and all(n.startswith("~canary_") and n.endswith(".txt") for n in names)


def test_the_check_never_deletes_or_rewrites_a_tampered_decoy(env):
    rc.check()
    p = env / "vault" / "00 - Inbox" / "~canary_01.txt"
    p.write_text("tampered", encoding="utf-8")
    rc.check()
    assert p.read_text(encoding="utf-8") == "tampered"


def test_exactly_twenty_modified_notes_is_fine_and_twenty_one_is_not(env):
    rc.check()
    for n in range(20):
        (env / "vault" / ("n%d.md" % n)).write_text("x", encoding="utf-8")
    assert rc.check()["alerts_raised"] == 0
    (env / "vault" / "n20.md").write_text("x", encoding="utf-8")
    assert rc.check()["alerts_raised"] == 1


def test_notes_older_than_five_minutes_do_not_count(env):
    rc.check()
    old = time.time() - 600
    for n in range(30):
        p = env / "vault" / ("old%d.md" % n)
        p.write_text("x", encoding="utf-8")
        os.utime(p, (old, old))
    assert rc.check()["alerts_raised"] == 0
