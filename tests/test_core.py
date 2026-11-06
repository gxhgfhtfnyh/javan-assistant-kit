"""Tests for the core/ copy step of generate.py (the harness scripts, skills and hook wiring that go into the new folder)."""
import json
import os
import sys

import pytest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)
import generate as G  # noqa: E402

ANSWERS = {"name": "Sam", "goals": "get organised", "os": "windows"}


@pytest.fixture
def kit(tmp_path):
    """A minimal kit: the real questions, no vault templates, and a small core tree."""
    k = tmp_path / "kit"
    (k / "core" / "scripts").mkdir(parents=True)
    (k / "core" / ".claude").mkdir()
    with open(os.path.join(KIT, "questions.json"), encoding="utf-8") as f:
        (k / "questions.json").write_text(f.read(), encoding="utf-8")
    (k / "structure.json").write_text(json.dumps({"folders": ["logs"]}), encoding="utf-8")
    (k / "core" / "scripts" / "tool.py").write_bytes(b"print('hi')\r\nprint('crlf kept')\r\n")
    (k / "core" / "scripts" / "data.bin").write_bytes(bytes(range(256)))
    (k / "core" / ".claude" / "settings.json.tpl").write_text('{"cmd": "python \\"{{root_path}}/scripts/tool.py\\" for {{name}}"}', encoding="utf-8")
    (k / "core" / "scripts" / "__pycache__").mkdir()
    (k / "core" / "scripts" / "__pycache__" / "tool.cpython-312.pyc").write_bytes(b"junk")
    (k / "core" / "scripts" / "stale.pyc").write_bytes(b"junk")
    return k


def test_core_files_are_copied_byte_for_byte_and_caches_are_skipped(kit, tmp_path):
    out = tmp_path / "out"
    rep = G.generate(kit, ANSWERS, out)
    assert (out / "scripts" / "tool.py").read_bytes() == b"print('hi')\r\nprint('crlf kept')\r\n"
    assert (out / "scripts" / "data.bin").read_bytes() == bytes(range(256))
    assert sorted(p for p in rep["created"] if p.startswith("scripts/")) == ["scripts/data.bin", "scripts/tool.py"]
    assert not (out / "scripts" / "__pycache__").exists() and not (out / "scripts" / "stale.pyc").exists()


def test_tpl_files_in_core_are_rendered_with_the_folders_absolute_path_and_lose_the_suffix(kit, tmp_path):
    out = tmp_path / "out"
    rep = G.generate(kit, ANSWERS, out)
    assert ".claude/settings.json" in rep["created"] and not (out / ".claude" / "settings.json.tpl").exists()
    cfg = json.loads((out / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert cfg["cmd"] == 'python "%s/scripts/tool.py" for Sam' % out.resolve().as_posix()


def test_an_unknown_placeholder_in_a_core_template_is_an_error_not_a_blank(kit, tmp_path):
    (kit / "core" / "bad.txt.tpl").write_text("hello {{nope}}", encoding="utf-8")
    with pytest.raises(G.UnknownPlaceholder):
        G.generate(kit, ANSWERS, tmp_path / "out")


def test_a_second_run_skips_identical_files_and_keeps_edited_ones_unless_forced(kit, tmp_path):
    out = tmp_path / "out"
    G.generate(kit, ANSWERS, out)
    again = G.generate(kit, ANSWERS, out)
    assert again["created"] == [] and "scripts/tool.py" in again["skipped"]
    (out / "scripts" / "tool.py").write_text("my own edit", encoding="utf-8")
    kept = G.generate(kit, ANSWERS, out)
    assert "scripts/tool.py" in kept["skipped"] and (out / "scripts" / "tool.py").read_text(encoding="utf-8") == "my own edit"
    forced = G.generate(kit, ANSWERS, out, force=True)
    assert forced["overwritten"] == ["scripts/tool.py"]
    assert (out / "scripts" / "tool.py").read_bytes() == b"print('hi')\r\nprint('crlf kept')\r\n"


def test_a_kit_without_a_core_folder_still_generates(kit, tmp_path):
    import shutil
    shutil.rmtree(kit / "core")
    rep = G.generate(kit, ANSWERS, tmp_path / "out")
    assert "logs" in rep["folders"] and rep["created"] == []


def test_the_real_core_tree_has_the_hook_wiring_and_it_renders_to_valid_json(tmp_path):
    out = tmp_path / "out"
    G.generate(KIT, ANSWERS, out)
    cfg = json.loads((out / ".claude" / "settings.json").read_text(encoding="utf-8"))
    cmds = {ev: cfg["hooks"][ev][0]["hooks"][0]["command"] for ev in ("SessionStart", "PreToolUse", "Stop")}
    assert cmds["SessionStart"].endswith('scripts/session_start.py"') and cmds["PreToolUse"].endswith('scripts/send_gate.py"') and cmds["Stop"].endswith('scripts/stop_gate.py"')
    assert cfg["hooks"]["PreToolUse"][0]["matcher"] == "*"
    assert out.resolve().as_posix() in cmds["Stop"]


def test_a_logs_folder_inside_core_is_never_copied_and_the_kit_may_live_inside_a_folder_named_logs(kit, tmp_path):
    (kit / "core" / "logs").mkdir()
    (kit / "core" / "logs" / "intents.jsonl").write_text('{"id": "stray"}\n', encoding="utf-8")
    rep = G.generate(kit, ANSWERS, tmp_path / "out")
    assert not any(p.startswith("logs/") for p in rep["created"]) and not (tmp_path / "out" / "logs" / "intents.jsonl").exists()
    inside = tmp_path / "logs" / "kit_home"
    inside.mkdir(parents=True)
    import shutil
    shutil.copytree(kit, inside / "kit")
    rep2 = G.generate(inside / "kit", ANSWERS, tmp_path / "out2")
    assert "scripts/tool.py" in rep2["created"]
