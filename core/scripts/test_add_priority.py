# -*- coding: utf-8 -*-
"""Tests for add_priority.py: the layout reader, adding under Now/Waiting, closing to Done or the archive, and the safe-write rules."""
import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import add_priority as ap  # noqa: E402

TODAY = datetime.date(2026, 10, 31)

NOTE = """---
status: active
type: tracker
---
# Active Priorities

Newest first inside each section.

## Now
- [ ] **Alpha job.** do the alpha thing. *Done when:* x *Added:* 2026-10-01
- [ ] **Beta job** do beta *Added:* 2026-10-02
- [ ] **Alpha job two** similar name *Added:* 2026-10-03
  - [ ] a child step

## Waiting on Sam
(nothing yet)

## Done
(nothing yet)
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    (tmp_path / "vault").mkdir()
    return tmp_path


def make(env, text=NOTE):
    p = env / "vault" / "Active Priorities.md"
    p.write_bytes(text.encode("utf-8"))
    return p


def read(p):
    return Path(p).read_bytes().decode("utf-8")


def test_paths_follow_the_environment_at_call_time(env, monkeypatch, tmp_path):
    assert ap.priorities_path() == (env / "vault" / "Active Priorities.md").resolve()
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("HARNESS_VAULT", str(other))
    assert ap.priorities_path() == other.resolve() / "Active Priorities.md"
    assert ap.archive_path() == other.resolve() / "04 - Archive" / "Completed Priorities Archive.md"


@pytest.mark.parametrize("title,kind", [
    ("Now", "now"), ("Open Tasks", "now"), ("Waiting on Sam", "waiting"), ("waiting", "waiting"), ("Done", "done"),
    ("Completed Tasks", "done"), ("Done ✅", "done"), ("Active Priorities", None), ("Notes", None), ("", None),
])
def test_headings_are_recognised_by_their_first_word(title, kind):
    assert ap.kind_of_heading(title) == kind


def test_list_items_reads_sections_titles_dates_and_children(env):
    items = ap.list_items(read(make(env)).split("\n"))
    assert [i["title"] for i in items] == ["Alpha job.", "Beta job", "Alpha job two"]    # the indented child is not its own item
    assert all(i["kind"] == "now" for i in items)
    assert items[0]["added"] == datetime.date(2026, 10, 1)
    assert items[2]["end"] - items[2]["idx"] == 2                                          # the child line travels with its parent


def test_checkboxes_and_headings_inside_code_blocks_are_ignored():
    lines = ["## Now", "```", "## Done", "- [ ] **Fake**", "```", "- [ ] **Real**"]
    items = ap.list_items(lines)
    assert [i["title"] for i in items] == ["Real"] and items[0]["kind"] == "now"


def test_add_puts_the_new_item_at_the_top_of_now_with_an_added_date(env):
    p = make(env)
    assert ap.add_open_task("**Renew licence** before friday", today=TODAY) == "added"
    text = read(p)
    now = text.index("## Now")
    first = text[now:].split("\n")[1]
    assert first == "- [ ] **Renew licence** before friday *Added:* 2026-10-31"
    assert text.index("Renew licence") < text.index("Alpha job.")
    assert text.count("\n") == NOTE.count("\n") + 1                                         # exactly one line more


def test_add_keeps_an_added_date_the_text_already_has(env):
    make(env)
    ap.add_open_task("**Thing** *Added:* 2026-01-02", today=TODAY)
    line = [l for l in read(env / "vault" / "Active Priorities.md").split("\n") if "**Thing**" in l][0]
    assert line.count("Added") == 1 and "2026-01-02" in line


def test_add_waiting_replaces_the_placeholder_and_keeps_the_blank_line(env):
    p = make(env)
    ap.add_open_task("**Wait for quote**", section="waiting", today=TODAY)
    text = read(p)
    assert "## Waiting on Sam\n- [ ] **Wait for quote** *Added:* 2026-10-31\n\n## Done\n(nothing yet)" in text
    assert text.count("(nothing yet)") == 1


def test_add_into_an_empty_now_section_without_placeholder(env):
    p = make(env, "# A\n\n## Now\n\n## Done\n(nothing yet)\n")
    ap.add_open_task("**One**", today=TODAY)
    assert read(p) == "# A\n\n## Now\n- [ ] **One** *Added:* 2026-10-31\n\n## Done\n(nothing yet)\n"


def test_add_refuses_when_the_section_is_missing_and_writes_nothing(env):
    p = make(env, "# Active Priorities\n\n## Done\n- [x] old\n")
    before = p.read_bytes()
    with pytest.raises(ValueError, match="refusing to guess"):
        ap.add_open_task("**New**", today=TODAY)
    assert p.read_bytes() == before


def test_a_title_that_is_already_open_is_not_added_twice(env):
    p = make(env)
    assert ap.add_open_task("**beta job**", today=TODAY) == "duplicate"        # case does not matter
    assert read(p) == NOTE


def test_a_title_closed_earlier_may_be_added_again(env):
    make(env, "## Now\n\n## Done\n- [x] **Beta job** old *Closed:* 2026-01-01\n")
    assert ap.add_open_task("**Beta job**", today=TODAY) == "added"


def test_empty_text_and_bad_section_are_refused(env):
    make(env)
    with pytest.raises(ValueError):
        ap.add_open_task("   \n ", today=TODAY)
    with pytest.raises(ValueError):
        ap.add_open_task("**x**", section="done", today=TODAY)


def test_text_is_forced_onto_one_line_so_it_cannot_inject_a_heading(env):
    p = make(env)
    ap.add_open_task("**Sneaky**\n## Done\n- [x] fake\x00", today=TODAY)
    text = read(p)
    assert text.count("\n## Done") == 1 and "\x00" not in text
    assert "- [ ] **Sneaky** ## Done - [x] fake *Added:* 2026-10-31" in text


def test_a_leading_checkbox_in_the_text_is_not_doubled(env):
    p = make(env)
    ap.add_open_task("- [ ] **Boxed**", today=TODAY)
    assert "- [ ] **Boxed** *Added" in read(p) and "- [ ] - [ ]" not in read(p)


def test_missing_note_and_non_utf8_note_are_reported_not_rewritten(env):
    with pytest.raises(ap.NoteMissing):
        ap.add_open_task("**x**", today=TODAY)
    p = env / "vault" / "Active Priorities.md"
    p.write_bytes(b"## Now\n\xff\xfe broken\n")
    with pytest.raises(ap.NoteError, match="UTF-8"):
        ap.add_open_task("**x**", today=TODAY)
    assert p.read_bytes() == b"## Now\n\xff\xfe broken\n"


def test_a_huge_note_is_refused(env, monkeypatch):
    make(env)
    monkeypatch.setattr(ap, "MAX_NOTE_BYTES", 10)
    with pytest.raises(ap.NoteError, match="not touching"):
        ap.add_open_task("**x**", today=TODAY)


def test_crlf_and_bom_are_kept(env):
    p = make(env, NOTE.replace("\n", "\r\n"))
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
    ap.add_open_task("**Crlf**", today=TODAY)
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert b"**Crlf**" in raw


def test_close_moves_exactly_the_matching_item_to_done_checked_and_dated(env):
    p = make(env)
    assert ap.close_task("Alpha job", today=TODAY) is True                       # trailing full stop in the note's bold title is ignored
    text = read(p)
    now_part, done_part = text.split("## Done")
    assert "**Alpha job.**" not in now_part and "**Alpha job two**" in now_part and "**Beta job**" in now_part
    assert "- [x] **Alpha job.** do the alpha thing. *Done when:* x *Added:* 2026-10-01 *Closed:* 2026-10-31" in done_part
    assert "(nothing yet)" not in done_part


def test_close_takes_the_childrens_lines_along(env):
    p = make(env)
    assert ap.close_task("alpha job two", today=TODAY)
    text = read(p)
    assert "a child step" in text.split("## Done")[1] and "a child step" not in text.split("## Done")[0]


def test_close_unknown_title_or_checked_item_changes_nothing(env):
    p = make(env, "## Now\n- [x] **Finished** x\n\n## Done\n")
    before = p.read_bytes()
    assert ap.close_task("Finished", today=TODAY) is False
    assert ap.close_task("Gamma", today=TODAY) is False
    assert p.read_bytes() == before
    with pytest.raises(ValueError):
        ap.close_task("  ")


def test_a_longer_title_with_the_same_start_is_not_taken_for_the_short_one(env):
    p = make(env, "## Now\n- [ ] **Alpha job two** long one first\n- [ ] **Alpha job** the short one\n\n## Done\n")
    assert ap.close_task("Alpha job", today=TODAY) is True
    now_part = read(p).split("## Done")[0]
    assert "**Alpha job two**" in now_part and "the short one" not in now_part


def test_closing_the_last_item_puts_the_placeholder_back(env):
    p = make(env, "## Now\n- [ ] **Only** x\n\n## Done\n(nothing yet)\n")
    ap.close_task("Only", today=TODAY)
    assert read(p) == "## Now\n(nothing yet)\n\n## Done\n- [x] **Only** x *Closed:* 2026-10-31\n"


def test_close_creates_a_done_section_when_there_is_none(env):
    p = make(env, "## Now\n- [ ] **Only** x\n")
    ap.close_task("Only", today=TODAY)
    assert read(p).endswith("\n## Done\n- [x] **Only** x *Closed:* 2026-10-31\n")


def test_close_to_archive_writes_the_archive_first_with_a_dated_block(env):
    p = make(env)
    assert ap.close_task("Alpha job", to="archive", today=TODAY)
    assert ap.close_task("Beta job", to="archive", today=TODAY)
    arch = read(ap.archive_path())
    assert arch.startswith("---\nstatus: archive") and arch.count("## 2026-10-31") == 1
    assert "- [x] **Alpha job.**" in arch and "- [x] **Beta job**" in arch
    assert "**Alpha job.**" not in read(p) and "**Beta job**" not in read(p)
    ap.close_task("Alpha job two", to="archive", today=datetime.date(2026, 11, 2))
    assert "## 2026-11-02" in read(ap.archive_path())


def test_archive_write_failure_leaves_the_priorities_note_alone(env, monkeypatch):
    p = make(env)
    before = p.read_bytes()

    def boom(*a, **k):
        raise ap.NoteError("disk full")
    monkeypatch.setattr(ap, "append_to_archive", boom)
    with pytest.raises(ap.NoteError):
        ap.close_task("Alpha job", to="archive", today=TODAY)
    assert p.read_bytes() == before


def test_atomic_write_retries_a_locked_file_then_gives_up(env, monkeypatch):
    calls = []

    def locked(src, dst):
        calls.append(1)
        raise PermissionError("locked")
    monkeypatch.setattr(ap.os, "replace", locked)
    monkeypatch.setattr(ap.time, "sleep", lambda s: None)
    target = env / "vault" / "x.md"
    with pytest.raises(ap.NoteError, match="cannot write"):
        ap.atomic_write(target, b"data")
    assert len(calls) == 5 and not target.exists()
    assert [f.name for f in (env / "vault").iterdir()] == []                          # the temp file was cleaned up


def test_format_item_builds_title_detail_and_done_when():
    assert ap.format_item("Renew", "before the 15th", "renewed") == "**Renew** before the 15th *Done when:* renewed"
    assert ap.format_item("**Own** style") == "**Own** style"


def test_cli_add_close_and_errors(env, capsys):
    p = make(env)
    assert ap.main(["Renew licence", "--detail", "soon"]) == 0
    assert "added: Renew licence" in capsys.readouterr().out and "**Renew licence** soon" in read(p)
    assert ap.main(["Renew licence"]) == 0
    assert "already open" in capsys.readouterr().out
    assert ap.main(["--close", "Renew licence"]) == 0
    assert ap.main(["--close", "Renew licence"]) == 1
    assert "no open item" in capsys.readouterr().out
    assert ap.main([]) == 1
    p.unlink()
    assert ap.main(["x"]) == 1
    assert "not found" in capsys.readouterr().out


def test_real_process_with_its_own_environment(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "Active Priorities.md").write_text(NOTE, encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_VAULT=str(vault), HARNESS_LOGS=str(tmp_path / "logs"))
    r = subprocess.run([sys.executable, "-B", str(HERE / "add_priority.py"), "Via process"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "added: Via process" in r.stdout
    assert "**Via process**" in (vault / "Active Priorities.md").read_text(encoding="utf-8")
