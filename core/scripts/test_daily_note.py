# -*- coding: utf-8 -*-
"""Tests for daily_note.py: the kit's folder layout, making a note from the template, the three kinds of append, the append-only guarantee,
line endings, failure paths and the real process."""
import io
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_note as d  # noqa: E402

NOW = datetime(2026, 10, 31, 9, 5)
TEMPLATE = """---
date: YYYY-MM-DD
type: daily
---
# YYYY-MM-DD

## Plan for today
-

## Session 1
(append a "## Session N" block each time the assistant starts a new session; never rewrite earlier blocks)
- What got done:
- Still open:
- Decisions made:
- Lessons (anything worth saving to the Lessons Pack):
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    folder = root / "vault" / "01 - Daily Notes"
    folder.mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    (folder / "Daily Note Template.md").write_text(TEMPLATE, encoding="utf-8")
    return SimpleNamespace(root=root, folder=folder, today=folder / "2026-10-31.md")


def lines_survive(old, new):
    """Every line of `old` is in `new`, in the same order (the note was only added to)."""
    it = iter(new.splitlines())
    return all(any(line == x for x in it) for line in old.splitlines())


# ---------- the layout and the template ----------
def test_today_is_a_flat_date_named_note_in_the_daily_notes_folder(env):
    assert d.note_path(NOW) == env.today
    assert d.template_path() == env.folder / "Daily Note Template.md"


def test_a_new_note_has_the_date_filled_in_and_no_session_block(env):
    text = d.new_note(NOW, TEMPLATE)
    assert text.startswith("---\ndate: 2026-10-31\ntype: daily\n---\n# 2026-10-31\n")
    assert "## Plan for today" in text and "Session" not in text and "YYYY" not in text and text.endswith("\n") and not text.endswith("\n\n")


def test_comments_and_unfilled_tokens_are_removed_and_known_tokens_filled():
    t = "# {{date}}\n<!-- hint -->\nBy {{assistant_name}} {{other}}\n## Session 1\nx\n"
    assert d.new_note(NOW, t) == "# 2026-10-31\nBy the assistant\n"


def test_without_a_template_a_built_in_skeleton_is_used():
    text = d.new_note(NOW, None)
    assert text.startswith("---\ndate: 2026-10-31\ntype: daily\n---\n# 2026-10-31\n") and "## Plan for today" in text


def test_the_session_skeleton_is_the_templates_label_lines_without_the_instruction():
    assert d.session_skeleton(TEMPLATE) == "- What got done:\n- Still open:\n- Decisions made:\n- Lessons (anything worth saving to the Lessons Pack):"
    assert d.session_skeleton("# no sessions here\n") == "" and d.session_skeleton(None) == ""


@pytest.mark.parametrize("block,expected", [
    ('## Session 1\n(append a "## Session N" block)\n- What got done:\n- Still open:\n', True),
    ("## Session 1\n", True),
    ("## Session 1\n- What got done: shipped it\n", False),
    ("## Session 1\n(instruction)\n- What got done:\nA free line of text\n", False),
    ("## Session 1\n- What got done:\n- Still open:\n", False),          # no instruction line: someone typed these
])
def test_placeholder_block_detection(block, expected):
    assert d.is_placeholder_block(block) is expected


# ---------- index lines ----------
def test_index_line_goes_after_the_last_bullet_of_the_index_block():
    text = "# T\n\n## Index\n- first\n- second\n\n## Plan\n-\n"
    out = d.add_index_line(text, "**Topic**: outcome")
    assert out == "# T\n\n## Index\n- first\n- second\n- **Topic**: outcome\n\n## Plan\n-\n"


def test_index_line_makes_the_block_after_the_heading_when_there_is_none():
    text = "---\ntype: daily\n---\n# 2026-10-31\n\n## Plan for today\n-\n"
    out = d.add_index_line(text, "- already a bullet\n with a newline")
    assert out.startswith("---\ntype: daily\n---\n# 2026-10-31\n\n## Index\n- already a bullet with a newline\n")
    assert lines_survive(text, out)


def test_index_line_in_an_empty_note_and_a_note_with_an_empty_index():
    assert d.add_index_line("", "x") == "\n## Index\n- x\n"
    assert d.add_index_line("# T\n## Index\n\n## Plan\n", "x") == "# T\n## Index\n- x\n\n## Plan\n"


# ---------- sessions ----------
def test_sessions_are_numbered_from_the_next_free_number():
    out = d.add_session("# T\n", "First", NOW, "- did a")
    assert out.endswith("\n\n## Session 1 - 09:05: First\n\n- did a\n")
    again = d.add_session(out, "Second", datetime(2026, 10, 31, 14, 30), "- did b")
    assert "## Session 2 - 14:30: Second" in again and lines_survive(out, again)
    gap = d.add_session("## Session 7 - x\n\n## Session 3\n", "Next", NOW)
    assert "## Session 8 - 09:05: Next" in gap


def test_a_session_without_a_body_gets_the_skeleton_and_topic_is_tidied():
    out = d.add_session("# T\n", "  Fixed \n the   sync " + "x" * 200, NOW, "", "- What got done:")
    head = [ln for ln in out.splitlines() if ln.startswith("## Session")][0]
    assert head.startswith("## Session 1 - 09:05: Fixed the sync xxx") and len(head) < 150
    assert out.rstrip().endswith("- What got done:")


def test_an_untouched_template_session_one_is_filled_in_not_numbered_past():
    note = d._fill(TEMPLATE, NOW)
    out = d.add_session(note, "Real work", NOW, "- shipped")
    assert "## Session 1 - 09:05: Real work" in out and "Session 2" not in out and "append a" not in out and "- shipped" in out
    assert out.startswith("---\ndate: 2026-10-31")


def test_a_session_one_with_content_is_kept_and_the_next_is_number_two():
    note = "# T\n\n## Session 1\n- What got done: shipped it\n"
    out = d.add_session(note, "More", NOW, "- x")
    assert "## Session 2 - 09:05: More" in out and lines_survive(note, out)


def test_plain_append_and_stdin_text_are_added_at_the_end_in_order():
    out = d.apply("# T\n\nend\n", NOW, index="- i", append="- tail line", body="piped text\nmore")
    assert out.endswith("end\n- tail line\npiped text\nmore\n") and "## Index\n- i" in out
    assert d.apply("# T\n", NOW, session="S", body="b").count("b") == 1       # the body went to the session, not twice


# ---------- running it ----------
def test_run_creates_today_from_the_template_then_appends_without_losing_anything(env):
    out = []
    assert d.run(session="Morning", body="- did a thing", now=NOW, out=out.append) == 0
    first = env.today.read_text(encoding="utf-8")
    assert any("created 2026-10-31.md from the template" in x for x in out)
    assert "# 2026-10-31" in first and "## Session 1 - 09:05: Morning" in first and "- did a thing" in first and "append a" not in first
    assert d.run(index="**Topic**: done", append="- tail", now=datetime(2026, 10, 31, 20, 0), out=out.append) == 0
    second = env.today.read_text(encoding="utf-8")
    assert lines_survive(first, second) and "- **Topic**: done" in second and second.rstrip().endswith("- tail")


def test_run_with_a_session_and_no_body_uses_the_templates_label_bullets(env):
    d.run(session="Quick", now=NOW, out=lambda *_: None)
    text = env.today.read_text(encoding="utf-8")
    assert "- What got done:" in text and "- Still open:" in text and "append a" not in text


def test_a_note_made_by_the_notes_app_from_the_template_gets_its_placeholder_filled(env):
    env.today.write_text(d._fill(TEMPLATE, NOW), encoding="utf-8")
    d.run(session="Real", body="- ok", now=NOW, out=lambda *_: None)
    text = env.today.read_text(encoding="utf-8")
    assert "## Session 1 - 09:05: Real" in text and "Session 2" not in text and text.count("## Session") == 1


def test_file_option_acts_on_another_file_and_creates_its_folder(env, tmp_path):
    target = tmp_path / "elsewhere" / "x.md"
    assert d.run(append="- hi", file=str(target), now=NOW, out=lambda *_: None) == 0
    assert target.read_text(encoding="utf-8").endswith("- hi\n") and not env.today.exists()


def test_dry_run_prints_the_addition_and_writes_nothing(env):
    out = []
    assert d.run(append="- hi", now=NOW, dry_run=True, out=out.append) == 0
    assert not env.today.exists() and any("dry run: would create" in x for x in out)
    env.today.write_text("# T\n", encoding="utf-8")
    out.clear()
    d.run(append="- hi", now=NOW, dry_run=True, out=out.append)
    assert env.today.read_text(encoding="utf-8") == "# T\n" and out[-1] == "- hi\n"


def test_crlf_notes_keep_their_line_endings(env):
    env.today.write_bytes(b"# T\r\n\r\nold\r\n")
    d.run(append="- new", now=NOW, out=lambda *_: None)
    raw = env.today.read_bytes()
    assert raw == b"# T\r\n\r\nold\r\n- new\r\n"


def test_a_note_that_is_not_utf8_is_refused_not_rewritten(env):
    env.today.write_bytes(b"# T\n\xff\xfe broken\n")
    out = []
    assert d.run(append="- x", now=NOW, out=out.append) == 2
    assert env.today.read_bytes() == b"# T\n\xff\xfe broken\n" and "cannot read" in out[0]


def test_an_unreadable_target_and_an_unwritable_one_exit_2_without_leftovers(env, monkeypatch):
    (env.folder / "dir.md").mkdir()
    out = []
    assert d.run(append="- x", file=str(env.folder / "dir.md"), now=NOW, out=out.append) == 2
    env.today.write_text("# T\n", encoding="utf-8")

    def refuse(src, dst):
        raise PermissionError("locked")
    monkeypatch.setattr(os, "replace", refuse)
    assert d.run(append="- x", now=NOW, out=out.append) == 2
    assert env.today.read_text(encoding="utf-8") == "# T\n"
    assert [p.name for p in env.folder.iterdir() if p.name.endswith(".tmp")] == []
    assert any("cannot write" in x for x in out)


def test_an_oversized_note_is_refused(env, monkeypatch):
    monkeypatch.setattr(d, "MAX_NOTE", 10)
    env.today.write_text("x" * 50, encoding="utf-8")
    assert d.run(append="- x", now=NOW, out=lambda *_: None) == 2


# ---------- the command line ----------
def test_main_with_nothing_to_do_prints_usage_and_exits_1(capsys):
    assert d.main([]) == 1
    assert "nothing to do" in capsys.readouterr().out


def test_main_reads_a_utf8_body_from_stdin(env, monkeypatch, capsys):
    class FakeIn:
        buffer = io.BytesIO("- café — done\r\n".encode("utf-8"))
    monkeypatch.setattr(sys, "stdin", FakeIn())
    assert d.main(["--session", "Unicode", "--stdin"]) == 0
    assert "- café — done" in d.note_path(datetime.now()).read_text(encoding="utf-8")


def test_main_with_empty_stdin_only_is_nothing_to_do(env, monkeypatch, capsys):
    class FakeIn:
        buffer = io.BytesIO(b"  \n")
    monkeypatch.setattr(sys, "stdin", FakeIn())
    assert d.main(["--stdin"]) == 1


def test_real_process_with_its_own_environment(tmp_path):
    root = tmp_path / "r"
    (root / "vault" / "01 - Daily Notes").mkdir(parents=True)
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, "-B", str(HERE / "daily_note.py"), "--session", "Smoke", "--index", "**A**: b", "--stdin"],
                       input="- café\n".encode("utf-8"), capture_output=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    notes = list((root / "vault" / "01 - Daily Notes").glob("*.md"))
    assert len(notes) == 1 and len(notes[0].stem) == 10
    text = notes[0].read_text(encoding="utf-8")
    assert "## Index\n- **A**: b" in text and ": Smoke" in text and "- café" in text
