# -*- coding: utf-8 -*-
"""Tests for keepsake_guard.py.

Every path used here lives under pytest tmp_path. The Windows permission tool (icacls) is replaced by an in-memory fake and _is_windows is forced, so the
Windows code path is exercised on every platform; the read-only bit is real (os.chmod on files under tmp_path). Nothing touches a real note or a real ACL.
"""
import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import keepsake_guard as kg  # noqa: E402

FROZEN = kg.FROZEN_FILE_DENY
ORIGINAL = b"Dear future reader,\nI am here.\n"
NOTES_OK = b"# Milestones\n\n- 2026-10-01 first\n\n## Add new entries above this line\n\nhow to add\n"
ANCHOR = b"## Add new entries above this line"


class FakeWin:
    """In-memory stand-in for icacls: remembers the deny entries per path."""

    def __init__(self):
        self.denies = {}
        self.icacls_calls = []
        self.returncode = 0

    def icacls(self, args):
        args = [str(a) for a in args]
        self.icacls_calls.append(args)
        target = args[0]
        if len(args) == 1:  # read-back form
            rights = sorted(self.denies.get(target, set()))
            out = f"{target} Everyone:(DENY)({','.join(rights)})\n" if rights else f"{target} Everyone:(I)(F)\n"
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        if args[1] == "/deny" and self.returncode == 0:
            rights = args[2].split(":(", 1)[1].rstrip(")").split(",")
            self.denies.setdefault(target, set()).update(rights)
        elif args[1] == "/remove:d":
            self.denies.pop(target, None)
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="")


def make_config(root, protect=False, notify=False):
    root = Path(root)
    return {
        "keepsakes": [
            {"name": "letter", "path": str(root / "live" / "letter.md"), "mode": "frozen"},
            {"name": "notes", "path": str(root / "live" / "notes.md"), "mode": "append_only"},
        ],
        "sealed_dirs": [str(root / "sealed1"), str(root / "sealed2"), str(root / "sealed3")],
        "manifest": str(root / "manifest.json"),
        "rejected_dir": str(root / "rejected"),
        "log": str(root / "logs" / "guard.jsonl"),
        "notify": notify,
        "protect": protect,
        "problems": [],
    }


def put(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def live_file(cfg, name):
    return Path(next(k["path"] for k in cfg["keepsakes"] if k["name"] == name))


def sealed_file(cfg, idx, name):
    return Path(cfg["sealed_dirs"][idx]) / f"{name}.sealed"


@pytest.fixture(autouse=True)
def sandbox(monkeypatch, tmp_path):
    win = FakeWin()
    monkeypatch.setattr(kg, "_is_windows", lambda: True)
    monkeypatch.setattr(kg, "_icacls", win.icacls)
    monkeypatch.setattr(kg, "LOGS", tmp_path / "harness" / "logs")
    monkeypatch.setattr(kg, "ROOT", tmp_path / "harness")
    monkeypatch.setattr(kg, "VAULT", tmp_path / "harness" / "vault")
    return SimpleNamespace(win=win)


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(kg, "send_alert", lambda text, intent_kind=None, tier=1, value=3, dedupe=None: sent.append(dict(text=text, kind=intent_kind, tier=tier, value=value, dedupe=dedupe)) or True)
    return sent


# ---- module shape and pure helpers -----------------------------------------------------------------------------------------------

def test_module_has_no_import_time_side_effects():
    tree = ast.parse((HERE / "keepsake_guard.py").read_text(encoding="utf-8"))
    allowed = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef, ast.Assign, ast.If)
    offenders = [(type(n).__name__, n.lineno) for n in tree.body
                 if not isinstance(n, allowed) and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assert offenders == []


def test_sha_and_read_helpers(tmp_path):
    assert kg.sha(ORIGINAL) == hashlib.sha256(ORIGINAL).hexdigest() and kg.sha(None) is None
    p = put(tmp_path / "a.md", b"x")
    assert kg.read(p) == b"x" and kg.read(tmp_path / "missing.md") is None


def test_read_raises_for_a_folder_in_the_way_but_read_soft_calls_it_missing(tmp_path):
    d = tmp_path / "folder"
    d.mkdir()
    with pytest.raises(OSError):
        kg.read(d)
    assert kg.read_soft(d) is None


def test_read_refuses_a_file_over_the_size_cap(tmp_path, monkeypatch):
    p = put(tmp_path / "big.md", b"x" * 100)
    monkeypatch.setattr(kg, "MAX_BYTES", 50)
    with pytest.raises(ValueError):
        kg.read(p)
    assert kg.read_soft(p) is None


def test_norm_lines_treats_crlf_cr_and_trailing_spaces_alike():
    assert kg.norm_lines(b"a  \r\nb\rc\n") == ["a", "b", "c", ""]


def test_is_superset_accepts_lines_added_anywhere_and_trailing_space_only():
    sealed = b"one\ntwo\n"
    assert kg.is_superset(b"zero\none\nmid\ntwo\nend\n", sealed) and kg.is_superset(b"one  \ntwo\n", sealed)


def test_is_superset_rejects_removed_altered_and_under_counted_lines():
    sealed = b"one\ntwo\n"
    assert not kg.is_superset(b"one\n", sealed) and not kg.is_superset(b"one\nTWO\n", sealed) and not kg.is_superset(b"", sealed)
    assert not kg.is_superset(b"x\n", b"x\nx\n")  # duplicates count


def test_write_in_place_creates_parents_and_truncates(tmp_path):
    p = tmp_path / "deep" / "x.md"
    kg.write_in_place(p, b"long long content")
    kg.write_in_place(p, b"short")
    assert p.read_bytes() == b"short"


def test_write_atomic_replaces_and_leaves_no_temp_file(tmp_path):
    p = tmp_path / "sub" / "m.json"
    kg.write_atomic(p, b"one")
    kg.write_atomic(p, b"two")
    assert p.read_bytes() == b"two" and [x.name for x in p.parent.iterdir()] == ["m.json"]


# ---- config ------------------------------------------------------------------------------------------------------------------------

def test_the_default_config_guards_nothing_and_keeps_its_folders_inside_the_harness(tmp_path):
    cfg = kg.default_config()
    assert cfg["keepsakes"] == [] and cfg["protect"] is False
    root = str(tmp_path / "harness")
    assert all(d.startswith(root) for d in cfg["sealed_dirs"] + [cfg["manifest"], cfg["rejected_dir"], cfg["log"]])
    assert len(cfg["sealed_dirs"]) == 3 and len(set(cfg["sealed_dirs"])) == 3


def test_load_config_without_a_file_gives_the_defaults():
    cfg = kg.load_config()
    assert cfg["keepsakes"] == [] and cfg["problems"] == [] and cfg["notify"] is True


def test_load_config_resolves_relative_paths_and_reads_flags(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"keepsakes": [{"name": "letter", "path": "notes/letter.md", "mode": "frozen"}],
                             "sealed_dirs": ["s/a", str(tmp_path / "abs")], "protect": True, "notify": False, "manifest": "m/manifest.json"}), encoding="utf-8")
    cfg = kg.load_config(p)
    assert cfg["keepsakes"] == [{"name": "letter", "path": str(tmp_path / "harness" / "vault" / "notes" / "letter.md"), "mode": "frozen"}]
    assert cfg["sealed_dirs"] == [str(tmp_path / "harness" / "s" / "a"), str(tmp_path / "abs")]
    assert cfg["protect"] is True and cfg["notify"] is False and cfg["manifest"] == str(tmp_path / "harness" / "m" / "manifest.json")


def test_protect_is_on_only_for_a_real_true(tmp_path):
    for value in ("true", 1, "yes", None):
        assert kg.normalize_config({"protect": value})["protect"] is False


def test_bad_keepsake_entries_are_reported_and_left_out():
    cfg = kg.normalize_config({"keepsakes": [
        {"name": "ok", "path": "a.md", "mode": "frozen"},
        {"name": "../escape", "path": "b.md", "mode": "frozen"},
        {"name": "ok", "path": "c.md", "mode": "frozen"},
        {"name": "wrongmode", "path": "d.md", "mode": "readonly"},
        {"name": "nopath", "mode": "frozen"},
        "not an object",
    ]})
    assert [k["name"] for k in cfg["keepsakes"]] == ["ok"] and len(cfg["problems"]) == 5


def test_an_empty_sealed_dirs_list_falls_back_to_the_defaults_with_a_note():
    cfg = kg.normalize_config({"sealed_dirs": ["", 5]})
    assert len(cfg["sealed_dirs"]) == 3 and any("sealed_dirs" in p for p in cfg["problems"])


@pytest.mark.parametrize("raw", ["{broken", "[1, 2]"])
def test_load_config_raises_for_a_file_that_is_not_a_json_object(tmp_path, raw):
    p = tmp_path / "cfg.json"
    p.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        kg.load_config(p)


def test_load_config_refuses_an_oversized_file(tmp_path, monkeypatch):
    p = tmp_path / "cfg.json"
    p.write_text("{}" + " " * 100, encoding="utf-8")
    monkeypatch.setattr(kg, "MAX_CONFIG_BYTES", 10)
    with pytest.raises(ValueError):
        kg.load_config(p)


# ---- permission helpers (icacls faked, read-only bit real) -----------------------------------------------------------------------

def test_deny_rights_reads_only_everyone_deny_entries(monkeypatch):
    monkeypatch.setattr(kg, "_icacls", lambda args: SimpleNamespace(stdout="C:/x Everyone:(DENY)(DE,WD)\n  Administrators:(I)(F)\n"))
    assert kg.deny_rights("C:/x") == {"DE", "WD"}
    monkeypatch.setattr(kg, "_icacls", lambda args: SimpleNamespace(stdout="x Everyone:(I)(RX)\n"))
    assert kg.deny_rights("x") == set()


def test_deny_rights_sees_an_inherited_deny_entry(monkeypatch):
    monkeypatch.setattr(kg, "_icacls", lambda args: SimpleNamespace(stdout="file Everyone:(OI)(CI)(DENY)(DE)\n"))
    assert kg.deny_rights("file") == {"DE"}


def test_deny_rights_on_a_non_english_windows_sees_nothing_a_known_limit(monkeypatch):
    monkeypatch.setattr(kg, "_icacls", lambda args: SimpleNamespace(stdout="file Tout le monde:(DENY)(DE)\n"))
    assert kg.deny_rights("file") == set()


def test_apply_deny_adds_rights_once(sandbox, tmp_path):
    p = put(tmp_path / "f.md", b"x")
    assert kg.apply_deny(p, ("DE", "WD")) is True and kg.apply_deny(p, ("DE",)) is False
    deny_calls = [c for c in sandbox.win.icacls_calls if "/deny" in c]
    assert len(deny_calls) == 1 and deny_calls[0][2] == "*S-1-1-0:(DE,WD)"
    assert sandbox.win.denies[str(p)] == {"DE", "WD"}


def test_apply_deny_reports_false_when_icacls_fails(sandbox, tmp_path):
    p = put(tmp_path / "f.md", b"x")
    sandbox.win.returncode = 5
    assert kg.apply_deny(p, ("DE",)) is False and str(p) not in sandbox.win.denies


def test_protect_then_unprotect_round_trip(sandbox, tmp_path):
    p = put(tmp_path / "f.md", b"x")
    assert kg.protect_file(p, FROZEN, True) is True and kg.is_readonly(p)
    assert sandbox.win.denies[str(p)] == set(FROZEN)
    assert kg.protect_file(p, FROZEN, True) is False  # already in place
    kg.unprotect_file(p)
    assert not kg.is_readonly(p) and str(p) not in sandbox.win.denies
    p.write_bytes(b"writable again")


def test_set_readonly_on_a_missing_file_does_nothing(tmp_path):
    kg.set_readonly(tmp_path / "nope.md", True)
    assert kg.is_readonly(tmp_path / "nope.md") is False


def test_off_windows_no_icacls_call_is_ever_made(monkeypatch, tmp_path):
    monkeypatch.setattr(kg, "_is_windows", lambda: False)
    monkeypatch.setattr(kg, "_icacls", lambda args: pytest.fail("icacls must not run off Windows"))
    p = put(tmp_path / "f.md", b"x")
    assert kg.deny_rights(p) == set() and kg.apply_deny(p, ("DE",)) is False and kg.protect_parent(tmp_path) is False
    kg.remove_deny(p)


def test_is_windows_follows_sys_platform(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(sys, "platform", "linux")
    assert kg._is_windows() is False
    monkeypatch.setattr(sys, "platform", "darwin")
    assert kg._is_windows() is False
    monkeypatch.setattr(sys, "platform", "win32")
    assert kg._is_windows() is True


def test_off_windows_the_guard_still_seals_and_restores_with_only_the_read_only_bit(monkeypatch, tmp_path):
    monkeypatch.setattr(kg, "_is_windows", lambda: False)
    monkeypatch.setattr(kg, "_icacls", lambda args: pytest.fail("icacls must not run off Windows"))
    cfg = make_config(tmp_path, protect=True)
    live = put(live_file(cfg, "letter"), ORIGINAL)
    kg.Guard(cfg).run()
    assert kg.is_readonly(live) and kg.is_readonly(sealed_file(cfg, 0, "letter"))
    kg.set_readonly(live, False)
    live.write_bytes(b"tampered\n")
    events = kg.Guard(cfg).run()
    assert live.read_bytes() == ORIGINAL and any(e["kind"] == "restored" for e in events) and kg.is_readonly(live)


# ---- guard flows on fake keepsakes -----------------------------------------------------------------------------------------------

def test_first_run_seals_every_copy_and_writes_manifest(tmp_path):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    events = kg.Guard(cfg).run()
    assert any(e["kind"] == "sealed" and e["name"] == "letter" for e in events)
    assert all(sealed_file(cfg, i, "letter").read_bytes() == ORIGINAL for i in range(3))
    manifest = json.loads(Path(cfg["manifest"]).read_text(encoding="utf-8"))
    assert manifest["letter"]["sha256"] == hashlib.sha256(ORIGINAL).hexdigest() and manifest["letter"]["mode"] == "frozen"


def test_missing_live_file_is_restored_from_sealed_copy(tmp_path):
    cfg = make_config(tmp_path)
    live = put(live_file(cfg, "letter"), ORIGINAL)
    kg.Guard(cfg).run()
    live.unlink()
    events = kg.Guard(cfg).run()
    assert live.read_bytes() == ORIGINAL and any(e["kind"] == "restored" and e["name"] == "letter" for e in events)
    assert not Path(cfg["rejected_dir"]).exists()  # nothing was overwritten, so nothing to keep


def test_missing_live_and_no_sealed_copy_reports_error_and_creates_nothing(tmp_path):
    cfg = make_config(tmp_path)
    events = kg.Guard(cfg).run()
    assert {e["name"] for e in events if e["kind"] == "error"} == {"letter", "notes"}
    assert not live_file(cfg, "letter").exists() and not Path(cfg["manifest"]).exists()


def test_unchanged_keepsakes_produce_no_events(tmp_path):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    put(live_file(cfg, "notes"), NOTES_OK)
    kg.Guard(cfg).run()
    assert kg.Guard(cfg).run() == []


def test_frozen_tamper_is_restored_and_rejected_copy_kept(tmp_path):
    cfg = make_config(tmp_path)
    live = put(live_file(cfg, "letter"), ORIGINAL)
    kg.Guard(cfg).run()
    live.write_bytes(b"tampered\n")
    events = kg.Guard(cfg).run()
    assert live.read_bytes() == ORIGINAL and any(e["kind"] == "restored" and e["name"] == "letter" for e in events)
    rejected = list(Path(cfg["rejected_dir"]).rglob("letter.rejected"))
    assert len(rejected) == 1 and rejected[0].read_bytes() == b"tampered\n"


def test_append_only_note_that_only_grew_is_resealed(tmp_path):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    live = put(live_file(cfg, "notes"), NOTES_OK)
    kg.Guard(cfg).run()
    grown = NOTES_OK.replace(ANCHOR, b"- 2026-10-29 new\n\n" + ANCHOR)
    live.write_bytes(grown)
    events = kg.Guard(cfg).run()
    assert any(e["kind"] == "resealed" and e["name"] == "notes" for e in events) and live.read_bytes() == grown
    assert all(sealed_file(cfg, i, "notes").read_bytes() == grown for i in range(3)) and not Path(cfg["rejected_dir"]).exists()


@pytest.mark.parametrize("variant", [
    b"# Milestones\n\n## Add new entries above this line\n\nhow to add\n",  # the first bullet removed
    NOTES_OK.replace(b"first", b"changed"),                                 # the first bullet altered
], ids=["removed", "altered"])
def test_append_only_note_losing_or_changing_a_line_is_restored(tmp_path, variant):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    live = put(live_file(cfg, "notes"), NOTES_OK)
    kg.Guard(cfg).run()
    live.write_bytes(variant)
    events = kg.Guard(cfg).run()
    assert live.read_bytes() == NOTES_OK and any(e["kind"] == "restored" and e["name"] == "notes" for e in events)
    rejected = list(Path(cfg["rejected_dir"]).rglob("notes.rejected"))
    assert len(rejected) == 1 and rejected[0].read_bytes() == variant


def test_damaged_sealed_copy_is_repaired_from_majority(tmp_path):
    cfg = make_config(tmp_path)
    live = put(live_file(cfg, "letter"), ORIGINAL)
    kg.Guard(cfg).run()
    sealed_file(cfg, 1, "letter").write_bytes(b"garbage")
    events = kg.Guard(cfg).run()
    assert sealed_file(cfg, 1, "letter").read_bytes() == ORIGINAL and live.read_bytes() == ORIGINAL
    assert any(e["kind"] == "repaired" and e["name"] == "letter" for e in events)


def test_a_missing_sealed_copy_is_rewritten(tmp_path):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    kg.Guard(cfg).run()
    sealed_file(cfg, 2, "letter").unlink()
    kg.Guard(cfg).run()
    assert sealed_file(cfg, 2, "letter").read_bytes() == ORIGINAL


def test_without_majority_the_manifest_decides_which_sealed_copy_wins(tmp_path):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    kg.Guard(cfg).run()
    sealed_file(cfg, 0, "letter").write_bytes(b"one")
    sealed_file(cfg, 1, "letter").write_bytes(b"two")
    events = kg.Guard(cfg).run()
    assert sealed_file(cfg, 0, "letter").read_bytes() == ORIGINAL and sealed_file(cfg, 1, "letter").read_bytes() == ORIGINAL
    assert sum(1 for e in events if e["kind"] == "repaired") == 2


def test_status_reports_ok_grew_changed_and_missing(tmp_path):
    cfg = make_config(tmp_path)
    letter = put(live_file(cfg, "letter"), ORIGINAL)
    notes = put(live_file(cfg, "notes"), NOTES_OK)
    kg.Guard(cfg).run()
    st = kg.Guard(cfg).status()
    assert st["letter"]["state"] == "OK" and st["notes"]["state"] == "OK"
    notes.write_bytes(NOTES_OK + b"- 2026-10-29 new\n")
    assert kg.Guard(cfg).status()["notes"]["state"] == "GREW"
    notes.write_bytes(b"completely different\n")
    assert kg.Guard(cfg).status()["notes"]["state"] == "CHANGED"
    letter.unlink()
    assert kg.Guard(cfg).status()["letter"]["state"] == "MISSING"


def test_status_reports_unsealed_when_no_sealed_copy_exists_and_writes_nothing(tmp_path):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    st = kg.Guard(cfg).status()
    assert st["letter"]["state"] == "UNSEALED" and st["letter"]["sealed_majority"] is None
    assert not Path(cfg["manifest"]).exists() and not Path(cfg["sealed_dirs"][0]).exists()


def test_status_reports_an_unreadable_note_without_raising(tmp_path):
    cfg = make_config(tmp_path)
    live_file(cfg, "letter").mkdir(parents=True)
    assert kg.Guard(cfg).status()["letter"]["state"] == "UNREADABLE"


def test_dry_run_reports_restore_but_changes_nothing(tmp_path):
    cfg = make_config(tmp_path)
    live = put(live_file(cfg, "letter"), ORIGINAL)
    kg.Guard(cfg).run()
    live.write_bytes(b"tampered\n")
    events = kg.Guard(cfg, dry=True).run()
    assert any(e["kind"] == "restored" for e in events) and live.read_bytes() == b"tampered\n" and not Path(cfg["rejected_dir"]).exists()


def test_dry_run_on_a_fresh_setup_seals_nothing(tmp_path):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    events = kg.Guard(cfg, dry=True).run()
    assert any(e["kind"] == "sealed" for e in events)
    assert not Path(cfg["sealed_dirs"][0]).exists() and not Path(cfg["manifest"]).exists()


def test_one_keepsake_failing_does_not_stop_the_other(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    put(live_file(cfg, "notes"), NOTES_OK)
    original_check = kg.Guard.check

    def flaky_check(self, k):
        if k["name"] == "letter":
            raise RuntimeError("boom")
        return original_check(self, k)

    monkeypatch.setattr(kg.Guard, "check", flaky_check)
    events = kg.Guard(cfg).run()
    errors = [e for e in events if e["kind"] == "error"]
    assert len(errors) == 1 and errors[0]["name"] == "letter" and errors[0]["detail"].startswith("RuntimeError: boom")
    assert sealed_file(cfg, 0, "notes").exists()


def test_a_live_path_that_is_a_folder_is_an_error_not_a_restore(tmp_path):
    cfg = make_config(tmp_path)
    live_file(cfg, "letter").mkdir(parents=True)
    events = kg.Guard(cfg).run()
    assert any(e["kind"] == "error" and e["name"] == "letter" for e in events) and not any(e["kind"] == "restored" for e in events)


def test_protected_run_applies_frozen_and_append_denies(tmp_path, sandbox):
    cfg = make_config(tmp_path, protect=True)
    letter = put(live_file(cfg, "letter"), ORIGINAL)
    notes = put(live_file(cfg, "notes"), NOTES_OK)
    kg.Guard(cfg).run()
    win = sandbox.win
    assert win.denies[str(letter)] == set(FROZEN) and kg.is_readonly(letter)
    assert win.denies[str(notes)] == {"DE"} and not kg.is_readonly(notes)
    assert win.denies[str(letter.parent)] == {"DC"}
    assert win.denies[str(sealed_file(cfg, 0, "letter"))] == set(FROZEN)
    assert [e for e in kg.Guard(cfg).run() if e["kind"] != "protected"] == []   # a second run on untouched files changes nothing
    kg.release(cfg, dry=False)


def test_a_protected_frozen_note_can_still_be_restored_after_a_tamper(tmp_path):
    cfg = make_config(tmp_path, protect=True)
    letter = put(live_file(cfg, "letter"), ORIGINAL)
    kg.Guard(cfg).run()
    kg.unprotect_file(letter)                      # what an administrator could do
    letter.write_bytes(b"tampered\n")
    events = kg.Guard(cfg).run()
    assert letter.read_bytes() == ORIGINAL and any(e["kind"] == "restored" for e in events) and kg.is_readonly(letter)
    kg.release(cfg, dry=False)


# ---- the policy when every sealed copy is gone: refuse to bless a changed file -------------------------------------------------

def _sealed_then_all_copies_gone(tmp_path):
    cfg = make_config(tmp_path)
    live = put(live_file(cfg, "letter"), ORIGINAL)
    put(live_file(cfg, "notes"), NOTES_OK)
    kg.Guard(cfg).run()
    for i in range(3):
        for name in ("letter", "notes"):
            sealed_file(cfg, i, name).unlink()
    return cfg, live


def test_changed_live_file_with_no_sealed_copies_is_refused_and_kept_as_evidence(tmp_path):
    cfg, live = _sealed_then_all_copies_gone(tmp_path)
    live.write_bytes(b"tampered\n")
    manifest_before = Path(cfg["manifest"]).read_bytes()
    events = kg.Guard(cfg).run()
    assert [e["name"] for e in events if e["kind"] == "refused"] == ["letter"]
    assert "sealed" not in {e["kind"] for e in events if e["name"] == "letter"}
    assert live.read_bytes() == b"tampered\n" and not any(sealed_file(cfg, i, "letter").exists() for i in range(3))
    assert Path(cfg["manifest"]).read_bytes() == manifest_before
    kept = list((Path(cfg["rejected_dir"]) / "unverified").glob("letter-*.live"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"tampered\n"


def test_the_refusal_repeats_every_cycle_without_piling_up_copies(tmp_path):
    cfg, live = _sealed_then_all_copies_gone(tmp_path)
    live.write_bytes(b"tampered\n")
    for _ in range(3):
        assert any(e["kind"] == "refused" for e in kg.Guard(cfg).run())
    assert len(list((Path(cfg["rejected_dir"]) / "unverified").glob("letter-*.live"))) == 1


def test_putting_the_original_back_lets_the_guard_reseal_itself(tmp_path):
    cfg, live = _sealed_then_all_copies_gone(tmp_path)
    live.write_bytes(b"tampered\n")
    kg.Guard(cfg).run()
    live.write_bytes(ORIGINAL)
    events = kg.Guard(cfg).run()
    assert "sealed" in {e["kind"] for e in events if e["name"] == "letter"} and "refused" not in {e["kind"] for e in events}
    assert all(sealed_file(cfg, i, "letter").read_bytes() == ORIGINAL for i in range(3))


def test_an_unchanged_live_file_with_no_sealed_copies_is_resealed(tmp_path):
    cfg, _live = _sealed_then_all_copies_gone(tmp_path)
    assert "refused" not in {e["kind"] for e in kg.Guard(cfg).run()}
    assert all(sealed_file(cfg, i, "letter").exists() for i in range(3))


def test_dry_run_refusal_writes_nothing(tmp_path):
    cfg, live = _sealed_then_all_copies_gone(tmp_path)
    live.write_bytes(b"tampered\n")
    events = kg.Guard(cfg, dry=True).run()
    assert any(e["kind"] == "refused" for e in events) and not (Path(cfg["rejected_dir"]) / "unverified").exists()


# ---- append_line -------------------------------------------------------------------------------------------------------------------

def test_append_line_adds_at_the_end_in_place_and_reseals(tmp_path):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    live = put(live_file(cfg, "notes"), NOTES_OK)
    kg.Guard(cfg).run()
    inode_before = os.stat(live).st_ino
    assert kg.append_line(cfg, "notes", "- 2026-10-29 new thing") == "added and re-sealed"
    assert live.read_bytes() == NOTES_OK + b"- 2026-10-29 new thing\n"
    assert sealed_file(cfg, 0, "notes").read_bytes() == live.read_bytes() and os.stat(live).st_ino == inode_before


def test_append_line_keeps_crlf_and_adds_a_missing_final_newline(tmp_path):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    live = put(live_file(cfg, "notes"), b"# N\r\n- first")
    kg.Guard(cfg).run()
    kg.append_line(cfg, "notes", "second")
    data = live.read_bytes()
    assert data == b"# N\r\n- first\r\nsecond\r\n" and data.count(b"\r\n") == data.count(b"\n")


def test_append_line_collapses_whitespace_and_newlines_to_one_line(tmp_path):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    live = put(live_file(cfg, "notes"), b"a\n")
    kg.Guard(cfg).run()
    kg.append_line(cfg, "notes", "x\n\ny\t z ")
    assert live.read_bytes() == b"a\nx y z\n"


def test_append_line_reports_already_there_and_leaves_the_file_alone(tmp_path):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    live = put(live_file(cfg, "notes"), NOTES_OK)
    kg.Guard(cfg).run()
    kg.append_line(cfg, "notes", "- dup entry")
    before = live.read_bytes()
    assert kg.append_line(cfg, "notes", "- dup entry") == "already there" and live.read_bytes() == before


def test_a_line_that_is_only_a_prefix_of_an_old_line_is_still_added(tmp_path):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    put(live_file(cfg, "notes"), b"- 2026-10-15 foo bar baz\n")
    kg.Guard(cfg).run()
    assert kg.append_line(cfg, "notes", "- 2026-10-15 foo bar") == "added and re-sealed"


@pytest.mark.parametrize("name,text", [("nope", "x"), ("letter", "x"), ("notes", "   ")], ids=["unknown", "frozen", "empty"])
def test_append_line_rejects_bad_requests(tmp_path, name, text):
    cfg = make_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    put(live_file(cfg, "notes"), NOTES_OK)
    with pytest.raises(ValueError):
        kg.append_line(cfg, name, text)


def test_append_line_to_a_missing_note_is_refused_not_created(tmp_path):
    cfg = make_config(tmp_path)
    with pytest.raises(ValueError):
        kg.append_line(cfg, "notes", "x")
    assert not live_file(cfg, "notes").exists()


# ---- release -----------------------------------------------------------------------------------------------------------------------

def test_release_lists_then_removes_exactly_what_the_guard_added(tmp_path, sandbox):
    cfg = make_config(tmp_path, protect=True)
    letter = put(live_file(cfg, "letter"), ORIGINAL)
    put(live_file(cfg, "notes"), NOTES_OK)
    kg.Guard(cfg).run()
    listed = kg.release(cfg, dry=True)
    assert str(letter) in listed and str(letter.parent) in listed and str(sealed_file(cfg, 0, "letter")) in listed
    assert kg.is_readonly(letter) and sandbox.win.denies[str(letter)]          # a listing changed nothing
    assert kg.release(cfg, dry=False) == listed
    assert not kg.is_readonly(letter) and sandbox.win.denies == {} and kg.release(cfg, dry=True) == []


# ---- log and alerts ----------------------------------------------------------------------------------------------------------------

def test_log_run_writes_summary_lines_and_skips_dry_runs(tmp_path):
    cfg = make_config(tmp_path)
    kg.log_run(cfg, [], dry=True)
    assert not Path(cfg["log"]).exists()
    kg.log_run(cfg, [], dry=False)
    kg.log_run(cfg, [{"kind": "protected", "name": "letter", "detail": ""}], dry=False)
    kg.log_run(cfg, [{"kind": "restored", "name": "letter", "detail": "x"}, {"kind": "repaired", "name": "letter", "detail": "y"}], dry=False)
    lines = [json.loads(x) for x in Path(cfg["log"]).read_text(encoding="utf-8").splitlines()]
    assert [line["result"] for line in lines] == ["ok", "ok", "repaired,restored"] and lines[2]["events"][0]["detail"] == "x"


def test_log_run_trims_to_last_2000_lines_once_past_3000(tmp_path):
    cfg = make_config(tmp_path)
    log = Path(cfg["log"])
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(f"old {i}\n" for i in range(3001)), encoding="utf-8")
    kg.log_run(cfg, [], dry=False)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2000 and lines[0] == "old 1002" and json.loads(lines[-1])["result"] == "ok"


def test_a_restore_raises_one_informational_work_item(tmp_path, alerts):
    cfg = make_config(tmp_path, notify=True)
    kg.notify_events(cfg, [{"kind": "protected", "name": "letter", "detail": "permissions re-applied"}])
    assert alerts == []
    kg.notify_events(cfg, [{"kind": "restored", "name": "letter", "detail": "the frozen file had changed"}])
    assert len(alerts) == 1 and alerts[0]["kind"] == "keepsake_restored" and alerts[0]["tier"] == 1
    assert "letter" in alerts[0]["text"] and "nothing is lost" in alerts[0]["text"]


def test_an_error_raises_a_question_tier_work_item(tmp_path, alerts):
    kg.notify_events(make_config(tmp_path, notify=True), [{"kind": "error", "name": "notes", "detail": "OSError: nope"}])
    assert len(alerts) == 1 and alerts[0]["kind"] == "keepsake_error" and alerts[0]["tier"] == 2 and "notes" in alerts[0]["text"]


def test_notify_false_sends_nothing(tmp_path, alerts):
    kg.notify_events(make_config(tmp_path, notify=False), [{"kind": "error", "name": "letter", "detail": "x"}, {"kind": "restored", "name": "letter", "detail": "x"}])
    assert alerts == []


def test_the_refusal_alert_goes_out_once_a_day(tmp_path, alerts):
    cfg = make_config(tmp_path, notify=True)
    events = [{"kind": "refused", "name": "letter", "detail": "x"}]
    kg.notify_events(cfg, events)
    kg.notify_events(cfg, events)
    assert len(alerts) == 1 and "not re-sealed" in alerts[0]["text"] and alerts[0]["kind"] == "keepsake_refused" and alerts[0]["tier"] == 2
    assert kg._first_refusal(cfg, "letter", now=__import__("time").time() + 25 * 3600) is True      # a day later it may speak again


def test_send_alert_writes_a_line_through_notify(tmp_path):
    assert kg.send_alert("keepsake guard: test line", intent_kind="keepsake_error", tier=2, value=4, dedupe="t") is True
    assert "keepsake_guard: keepsake guard: test line" in (tmp_path / "harness" / "logs" / "alerts.txt").read_text(encoding="utf-8")


def test_send_alert_never_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "notify", None)
    assert kg.send_alert("x") is False


# ---- main --------------------------------------------------------------------------------------------------------------------------

def write_config(tmp_path, protect=False, notify=False):
    cfg = make_config(tmp_path, protect=protect, notify=notify)
    path = tmp_path / "harness" / "logs" / kg.CONFIG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: cfg[k] for k in ("keepsakes", "sealed_dirs", "manifest", "rejected_dir", "log", "notify", "protect")}), encoding="utf-8")
    return cfg


def test_main_kill_switch_stops_everything(tmp_path, capsys):
    cfg = write_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    (kg.LOGS / kg.OFF_NAME).write_text("off", encoding="utf-8")
    assert kg.main(["--apply"]) == 0 and "off" in capsys.readouterr().out and not Path(cfg["manifest"]).exists()


def test_main_without_keepsakes_explains_and_exits_zero_for_a_cycle_but_one_for_an_explicit_command(capsys):
    assert kg.main([]) == 0
    assert "no keepsakes configured" in capsys.readouterr().out
    assert kg.main(["--append", "notes", "x"]) == 1


def test_main_default_run_is_a_dry_run_until_armed(tmp_path, capsys):
    cfg = write_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    put(live_file(cfg, "notes"), NOTES_OK)
    assert kg.main([]) == 0
    out = capsys.readouterr().out
    assert '"kind": "sealed"' in out and "dry run (nothing changed)" in out and kg.ON_NAME in out
    assert not Path(cfg["manifest"]).exists() and not Path(cfg["sealed_dirs"][0]).exists() and not Path(cfg["log"]).exists()


def test_main_acts_when_the_arming_file_exists_and_logs_the_cycle(tmp_path):
    cfg = write_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    put(live_file(cfg, "notes"), NOTES_OK)
    (kg.LOGS / kg.ON_NAME).write_text("on", encoding="utf-8")
    assert kg.main([]) == 0
    assert sealed_file(cfg, 0, "letter").read_bytes() == ORIGINAL and Path(cfg["manifest"]).exists()
    assert json.loads(Path(cfg["log"]).read_text(encoding="utf-8").splitlines()[0])["result"] == "sealed"


def test_main_apply_acts_without_the_arming_file_and_dry_run_overrides_the_arming_file(tmp_path):
    cfg = write_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    put(live_file(cfg, "notes"), NOTES_OK)
    (kg.LOGS / kg.ON_NAME).write_text("on", encoding="utf-8")
    assert kg.main(["--dry-run"]) == 0 and not Path(cfg["manifest"]).exists()
    (kg.LOGS / kg.ON_NAME).unlink()
    assert kg.main(["--apply"]) == 0 and Path(cfg["manifest"]).exists()


def test_main_restores_a_tampered_note_only_when_armed(tmp_path, capsys):
    cfg = write_config(tmp_path)
    live = put(live_file(cfg, "letter"), ORIGINAL)
    put(live_file(cfg, "notes"), NOTES_OK)
    kg.main(["--apply"])
    live.write_bytes(b"tampered\n")
    kg.main([])                                              # not armed: reports only
    assert live.read_bytes() == b"tampered\n" and '"kind": "restored"' in capsys.readouterr().out
    kg.main(["--apply"])
    assert live.read_bytes() == ORIGINAL


def test_main_status_prints_json_and_changes_nothing(tmp_path, capsys):
    cfg = write_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    assert kg.main(["--status"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["letter"]["state"] == "UNSEALED" and report["letter"]["mode"] == "frozen" and not Path(cfg["manifest"]).exists()


def test_main_append_adds_a_line_and_a_bad_request_exits_one(tmp_path, capsys):
    cfg = write_config(tmp_path)
    put(live_file(cfg, "letter"), ORIGINAL)
    live = put(live_file(cfg, "notes"), NOTES_OK)
    kg.main(["--apply"])
    assert kg.main(["--append", "notes", "- added by cli"]) == 0
    assert live.read_bytes().endswith(b"- added by cli\n") and "added and re-sealed" in capsys.readouterr().out
    assert kg.main(["--append", "letter", "x"]) == 1 and "not added" in capsys.readouterr().out


def test_main_release_lists_without_apply_and_removes_with_it(tmp_path, capsys):
    cfg = write_config(tmp_path, protect=True)
    letter = put(live_file(cfg, "letter"), ORIGINAL)
    put(live_file(cfg, "notes"), NOTES_OK)
    kg.main(["--apply"])
    assert kg.is_readonly(letter)
    assert kg.main(["--release"]) == 0 and kg.is_readonly(letter) and "listing only" in capsys.readouterr().out
    assert kg.main(["--release", "--apply"]) == 0 and not kg.is_readonly(letter)


def test_main_a_broken_config_exits_zero_for_a_cycle_and_one_for_an_explicit_command(tmp_path, capsys):
    kg.LOGS.mkdir(parents=True, exist_ok=True)
    (kg.LOGS / kg.CONFIG_NAME).write_text("{broken", encoding="utf-8")
    assert kg.main([]) == 0 and "cannot be used" in capsys.readouterr().out
    assert kg.main(["--release"]) == 1


def test_main_prints_config_problems_but_still_guards_the_valid_entries(tmp_path, capsys):
    cfg = write_config(tmp_path)
    data = json.loads((kg.LOGS / kg.CONFIG_NAME).read_text(encoding="utf-8"))
    data["keepsakes"].append({"name": "bad name!", "path": "x.md", "mode": "frozen"})
    (kg.LOGS / kg.CONFIG_NAME).write_text(json.dumps(data), encoding="utf-8")
    put(live_file(cfg, "letter"), ORIGINAL)
    put(live_file(cfg, "notes"), NOTES_OK)
    assert kg.main(["--apply"]) == 0
    assert "config: keepsake name" in capsys.readouterr().out and sealed_file(cfg, 0, "letter").exists()


def test_real_process_default_run_is_a_dry_run_and_status_reads_the_config(tmp_path):
    root = tmp_path / "root"
    logs = root / "logs"
    vault = root / "vault"
    (vault / "notes").mkdir(parents=True)
    logs.mkdir()
    (vault / "notes" / "letter.md").write_bytes(ORIGINAL)
    (logs / kg.CONFIG_NAME).write_text(json.dumps({"keepsakes": [{"name": "letter", "path": "notes/letter.md", "mode": "frozen"}]}), encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(logs), HARNESS_VAULT=str(vault), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "keepsake_guard.py")], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "dry run" in r.stdout
    assert not (root / "keepsakes").exists() and not (vault / ".sealed").exists()
    r = subprocess.run([sys.executable, str(HERE / "keepsake_guard.py"), "--status"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and json.loads(r.stdout)["letter"]["state"] == "UNSEALED"
