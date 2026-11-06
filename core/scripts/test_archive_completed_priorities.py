# -*- coding: utf-8 -*-
"""Tests for archive_completed_priorities.py: what counts as completed and old, the dry-run default, the move, and the failure paths."""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import add_priority as layout  # noqa: E402
import archive_completed_priorities as acp  # noqa: E402

TODAY = datetime.date(2026, 10, 31)

NOTE = """---
status: active
---
# Active Priorities

## Now
- [ ] **Open one** still going *Added:* 2026-01-01
- [x] **Ticked in Now** forgot to move *Closed:* 2026-08-01

## Waiting on Sam
(nothing yet)

## Done
- [x] **Recent** finished lately *Closed:* 2026-10-20
- [x] **Old closed.** finished long ago *Closed:* 2026-08-15
  - [x] its child step
- [x] **No date** nobody wrote a date
- [ ] **Unchecked in Done** odd but not completed
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    (tmp_path / "vault").mkdir()
    (tmp_path / "logs").mkdir()
    return tmp_path


def make(env, text=NOTE):
    p = env / "vault" / "Active Priorities.md"
    p.write_text(text, encoding="utf-8")
    return p


def titles(items):
    return [i["title"] for i in items]


def test_completed_items_are_checked_top_level_boxes_anywhere():
    items = acp.completed_items(NOTE.split("\n"))
    assert titles(items) == ["Ticked in Now", "Recent", "Old closed.", "No date"]            # the child box belongs to its parent


def test_checked_boxes_in_code_blocks_are_not_completed_items():
    assert acp.completed_items(["## Done", "```", "- [x] **Example**", "```"]) == []


def test_age_comes_from_the_closed_date_and_the_default_threshold_is_30_days(env):
    make(env)
    found = acp.find_archivable(today=TODAY)
    assert titles(found) == ["Ticked in Now", "Old closed."]
    assert [i["age_days"] for i in found] == [91, 77] and {i["source"] for i in found} == {"closed"}


def test_an_item_without_a_date_ages_from_the_day_it_was_first_seen(env):
    make(env)
    acp.find_archivable(today=TODAY)
    rec = json.loads((env / "logs" / "completed_priority_ages.json").read_text(encoding="utf-8"))
    assert rec["items"]["done:no date"] == "2026-10-31"
    assert "No date" not in titles(acp.find_archivable(today=TODAY + datetime.timedelta(days=29)))
    later = acp.find_archivable(today=TODAY + datetime.timedelta(days=30))
    hit = [i for i in later if i["title"] == "No date"][0]
    assert hit["age_days"] == 30 and hit["source"] == "first-seen"


def test_records_of_items_no_longer_checked_are_dropped(env):
    p = make(env)
    acp.find_archivable(today=TODAY)
    p.write_text(NOTE.replace("- [x] **No date** nobody wrote a date\n", ""), encoding="utf-8")
    acp.find_archivable(today=TODAY)
    rec = json.loads((env / "logs" / "completed_priority_ages.json").read_text(encoding="utf-8"))
    assert "done:no date" not in rec["items"]


def test_dry_run_changes_no_note_and_writes_no_archive(env, capsys):
    p = make(env)
    before = p.read_bytes()
    assert acp.main(["--days", "30"]) == 0
    out = capsys.readouterr().out
    assert "Would archive" in out and "2 completed item(s)" in out and "Old closed." in out
    assert p.read_bytes() == before and not layout.archive_path().exists()


def test_apply_moves_the_items_with_their_children_and_nothing_else(env):
    p = make(env)
    res = acp.run(apply=True, today=TODAY)
    assert res["moved"] == 2
    text = p.read_text(encoding="utf-8")
    for gone in ("Ticked in Now", "Old closed.", "its child step"):
        assert gone not in text
    for kept in ("**Open one**", "**Recent**", "**No date**", "**Unchecked in Done**", "## Waiting on Sam", "status: active"):
        assert kept in text
    arch = layout.archive_path().read_text(encoding="utf-8")
    assert "## 2026-10-31" in arch and "- [x] **Old closed.**" in arch and "  - [x] its child step" in arch and "- [x] **Ticked in Now**" in arch
    assert arch.startswith("---\nstatus: archive")


def test_a_second_run_finds_nothing_more(env):
    make(env)
    acp.run(apply=True, today=TODAY)
    again = acp.run(apply=True, today=TODAY)
    assert again == {"items": [], "moved": 0}


def test_an_emptied_done_section_gets_its_placeholder_back(env):
    p = make(env, "## Now\n\n## Done\n- [x] **Only** x *Closed:* 2026-01-01\n")
    acp.run(apply=True, today=TODAY)
    assert p.read_text(encoding="utf-8") == "## Now\n\n## Done\n(nothing yet)\n"


def test_the_threshold_is_adjustable(env):
    make(env)
    assert titles(acp.find_archivable(5, today=TODAY)) == ["Ticked in Now", "Old closed.", "Recent"]
    assert acp.find_archivable(500, today=TODAY) == []


def test_a_future_closed_date_is_age_zero(env):
    make(env, "## Done\n- [x] **Soon** x *Closed:* 2031-01-01\n")
    assert acp.find_archivable(0, today=TODAY)[0]["age_days"] == 0


def test_the_archive_is_written_first_so_a_failed_archive_leaves_the_note_alone(env, monkeypatch):
    p = make(env)
    before = p.read_bytes()

    def boom(*a, **k):
        raise layout.NoteError("disk full")
    monkeypatch.setattr(layout, "append_to_archive", boom)
    with pytest.raises(layout.NoteError):
        acp.run(apply=True, today=TODAY)
    assert p.read_bytes() == before


def test_a_failed_note_write_leaves_the_items_in_both_places(env, monkeypatch):
    p = make(env)
    before = p.read_bytes()

    real = layout.write_note

    def only_the_archive_succeeds(path, note):
        if Path(path) == p:
            raise layout.NoteError("locked")
        real(path, note)
    monkeypatch.setattr(layout, "write_note", only_the_archive_succeeds)
    with pytest.raises(layout.NoteError):
        acp.run(apply=True, today=TODAY)
    assert p.read_bytes() == before and "Old closed." in layout.archive_path().read_text(encoding="utf-8")


def test_corrupt_record_file_is_an_empty_record(env):
    make(env)
    (env / "logs" / "completed_priority_ages.json").write_text("{{ nope", encoding="utf-8")
    assert titles(acp.find_archivable(today=TODAY)) == ["Ticked in Now", "Old closed."]
    assert acp.load_records(env / "logs" / "does_not_exist.json") == {}


def test_missing_note_is_exit_2_for_a_person_and_0_for_the_scheduler(env, capsys):
    assert acp.main([]) == 2
    assert acp.main(["--apply", "--job"]) == 0
    assert "no priorities note" in capsys.readouterr().out


def test_unreadable_note_is_reported_and_left_alone(env, capsys):
    p = env / "vault" / "Active Priorities.md"
    p.write_bytes(b"## Done\n- [x] \xff\xfe\n")
    assert acp.main(["--apply"]) == 1
    assert "UTF-8" in capsys.readouterr().out and p.read_bytes() == b"## Done\n- [x] \xff\xfe\n"


def test_kill_switch_stops_the_scheduled_run_only(env, capsys):
    p = make(env)
    (env / "logs" / "archive_completed_priorities_off.txt").write_text("off", encoding="utf-8")
    assert acp.main(["--apply", "--job"]) == 0
    assert "off (archive_completed_priorities_off.txt present)" in capsys.readouterr().out
    assert p.read_text(encoding="utf-8") == NOTE
    assert acp.main([]) == 0 and "Would archive" in capsys.readouterr().out


def test_scheduled_apply_leaves_one_alert_line(env, capsys):
    make(env, "## Done\n- [x] **Ancient** x *Closed:* 2020-01-01\n")
    assert acp.main(["--apply", "--job"]) == 0
    assert "Moved 1 item(s)" in capsys.readouterr().out
    assert "moved 1 completed priority item(s)" in (env / "logs" / "alerts.txt").read_text(encoding="utf-8")
    assert not (env / "logs" / "intents.jsonl").exists()                                        # a courtesy line, not a work item


def test_crlf_note_keeps_its_line_endings(env):
    p = env / "vault" / "Active Priorities.md"
    p.write_bytes(NOTE.replace("\n", "\r\n").encode("utf-8"))
    acp.run(apply=True, today=TODAY)
    raw = p.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"") and b"Old closed." not in raw


def test_real_process_with_its_own_environment(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    note = vault / "Active Priorities.md"
    note.write_text("## Done\n- [x] **Ancient** x *Closed:* 2020-01-01\n", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_VAULT=str(vault), HARNESS_LOGS=str(tmp_path / "logs"))
    script = str(HERE / "archive_completed_priorities.py")
    r = subprocess.run([sys.executable, "-B", script], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "Would archive" in r.stdout and "Ancient" in note.read_text(encoding="utf-8")
    r = subprocess.run([sys.executable, "-B", script, "--apply"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "Moved 1 item(s)" in r.stdout and "Ancient" not in note.read_text(encoding="utf-8")
    assert "Ancient" in (vault / "04 - Archive" / "Completed Priorities Archive.md").read_text(encoding="utf-8")
