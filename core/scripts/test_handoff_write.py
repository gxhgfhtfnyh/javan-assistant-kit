# -*- coding: utf-8 -*-
"""Tests for handoff_write.py"""

import importlib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Add the scripts directory to path
sys.path.insert(0, str(Path(__file__).parent))

import handoff_write
import intent_store


@pytest.fixture
def hw(tmp_path, monkeypatch):
    """handoff_write reloaded against an empty temp logs folder. Returns (module, logs)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    importlib.reload(handoff_write)
    return handoff_write, logs


def add_intent(logs, what, tier=1, value=3, dedupe=None, **extra):
    """Append one intent to the temp store the way the real store does, then apply any field overrides."""
    row = intent_store.new_intent("test_kind", what, tier=tier, value=value, dedupe=dedupe or what)
    row.update(extra)
    intent_store.append(intent_store.INTENTS, row, logs=str(logs))
    return row


def stdin_bytes(monkeypatch, payload: bytes):
    class Mock:
        buffer = io.BytesIO(payload)

        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", Mock())


class TestSections:
    def test_all_sections_present(self, hw):
        mod, logs = hw
        (logs / "focus.md").write_text("Ship the quarterly report", encoding="utf-8")
        add_intent(logs, "Renew the domain", value=4)
        (logs / "alerts.txt").write_text("2026-10-31 stop_gate hourly cap reached\n", encoding="utf-8")
        text = mod.build_handoff()
        assert text.startswith("# Handoff (written ")
        assert "## Focus" in text and "Ship the quarterly report" in text
        assert "## Open intents" in text and "Renew the domain" in text
        assert "## Last alerts" in text and "hourly cap reached" in text

    def test_time_written_is_in_the_header(self, hw):
        mod, _ = hw
        import datetime as dt
        now = 1_800_000_000.0
        expected = dt.datetime.fromtimestamp(now).replace(microsecond=0).isoformat()
        assert mod.build_handoff(now=now).splitlines()[0] == "# Handoff (written %s)" % expected

    def test_intent_line_uses_the_store_fields(self, hw):
        mod, logs = hw
        row = add_intent(logs, "Back up the notes", tier=0, value=5)
        lines = mod.intent_lines()
        assert lines == ["- %s tier0 value5 Back up the notes" % row["id"]]


class TestCaps:
    def test_focus_is_capped_and_marked(self, hw):
        mod, logs = hw
        (logs / "focus.md").write_text("word " * 2000, encoding="utf-8")
        section = mod.focus_section()
        assert len(section) <= mod.FOCUS_CAP
        assert section.endswith("...")

    def test_short_focus_is_kept_whole(self, hw):
        mod, logs = hw
        (logs / "focus.md").write_text("line one\nline two\n", encoding="utf-8")
        assert mod.focus_section() == "line one\nline two"

    def test_whole_file_never_exceeds_the_cap_even_with_huge_inputs(self, hw):
        mod, logs = hw
        (logs / "focus.md").write_text("F" * 20000, encoding="utf-8")
        for n in range(40):
            add_intent(logs, ("long intent %d " % n) + "x" * 600, value=3 + (n % 3), dedupe="d%d" % n)
        (logs / "alerts.txt").write_text("\n".join("alert %d " % n + "y" * 500 for n in range(50)) + "\n", encoding="utf-8")
        text = mod.build_handoff()
        assert len(text) <= mod.MAX_CHARS == 2500
        assert text.startswith("# Handoff (written ")

    def test_hard_cap_cuts_on_a_line_end_and_marks_it(self, hw):
        mod, _ = hw
        text = "\n".join("line %03d" % n for n in range(1000)) + "\n"
        out = mod.hard_cap(text, 300)
        assert len(out) <= 300
        assert out.rstrip().endswith(mod.TRUNCATED_MARK)
        assert re.fullmatch(r"(line \d{3}\n)+" + re.escape(mod.TRUNCATED_MARK) + r"\n", out)

    def test_each_intent_line_is_capped(self, hw):
        mod, logs = hw
        add_intent(logs, "z" * 1000)
        assert all(len(ln) <= mod.INTENT_LINE_CAP for ln in mod.intent_lines())

    def test_alert_lines_are_capped_and_flattened(self, hw):
        mod, logs = hw
        (logs / "alerts.txt").write_text("a" * 1000 + "\n", encoding="utf-8")
        out = mod.alert_lines()
        assert len(out) == 1 and len(out[0]) <= mod.ALERT_LINE_CAP + 2


class TestMissingInputs:
    def test_missing_focus_file(self, hw):
        mod, logs = hw
        assert not (logs / "focus.md").exists()
        assert mod.focus_section() == "(no focus file)"
        assert "(no focus file)" in mod.build_handoff()

    def test_empty_focus_file(self, hw):
        mod, logs = hw
        (logs / "focus.md").write_text("  \n\n", encoding="utf-8")
        assert mod.focus_section() == "(no focus file)"

    def test_missing_intents_file(self, hw):
        mod, logs = hw
        assert not (logs / "intents.jsonl").exists()
        assert mod.intent_lines() == ["(none open)"]

    def test_missing_alerts_file(self, hw):
        mod, logs = hw
        assert mod.alert_lines() == ["(none)"]
        assert mod.tail_lines(logs / "nope.txt", 5) == []

    def test_everything_missing_still_builds_a_full_handoff(self, hw):
        mod, _ = hw
        text = mod.build_handoff()
        assert "(no focus file)" in text and "(none open)" in text and "(none)" in text

    def test_corrupt_intent_lines_are_skipped(self, hw):
        mod, logs = hw
        row = add_intent(logs, "Real one", value=4)
        with (logs / "intents.jsonl").open("a", encoding="utf-8") as f:
            f.write("not json at all\n[1, 2, 3]\n{\"no_id\": true}\n")
        lines = mod.intent_lines()
        assert lines == ["- %s tier1 value4 Real one" % row["id"]]

    def test_unreadable_intent_store_degrades_to_a_note(self, hw, monkeypatch):
        mod, _ = hw
        def boom(*a, **k):
            raise RuntimeError("broken")
        monkeypatch.setattr(intent_store, "read_jsonl", boom)
        assert mod.intent_lines() == ["(intent store unavailable)"]
        assert "(intent store unavailable)" in mod.build_handoff()


class TestAlerts:
    def test_only_the_last_five_alerts_are_kept(self, hw):
        mod, logs = hw
        (logs / "alerts.txt").write_text("".join("alert-%d\n" % n for n in range(8)), encoding="utf-8")
        out = mod.alert_lines()
        assert out == ["- alert-%d" % n for n in range(3, 8)]

    def test_blank_lines_are_ignored(self, hw):
        mod, logs = hw
        (logs / "alerts.txt").write_text("one\n\n\n  \ntwo\n\n", encoding="utf-8")
        assert mod.alert_lines() == ["- one", "- two"]

    def test_a_big_alerts_file_is_tailed_not_read_whole(self, hw):
        mod, logs = hw
        (logs / "alerts.txt").write_text("".join("old line %06d\n" % n for n in range(20000)) + "last-one\n", encoding="utf-8")
        out = mod.alert_lines()
        assert out[-1] == "- last-one" and len(out) == 5
        assert all(ln.startswith("- old line") or ln == "- last-one" for ln in out)  # the cut first line is dropped, so no half lines


class TestIntents:
    def test_top_five_by_rank_and_the_rest_left_out(self, hw):
        mod, logs = hw
        for n in range(8):
            add_intent(logs, "task-%d" % n, value=1 + n, dedupe="k%d" % n)
        lines = mod.intent_lines()
        assert len(lines) == mod.INTENTS_SHOWN == 5
        assert "value8 task-7" in lines[0]            # highest value first
        assert not any("task-0" in ln or "task-1" in ln or "task-2" in ln for ln in lines)

    def test_tier2_is_counted_not_listed(self, hw):
        mod, logs = hw
        add_intent(logs, "Ask the owner about the budget", tier=2, value=5)
        add_intent(logs, "Quiet housekeeping", tier=0, value=3)
        lines = mod.intent_lines()
        assert any("Quiet housekeeping" in ln for ln in lines)
        assert not any("budget" in ln for ln in lines)
        assert lines[-1] == "- 1 waiting on the owner (tier 2)"

    def test_a_claimed_intent_is_listed_first_as_doing(self, hw):
        mod, logs = hw
        add_intent(logs, "Open one", value=5, dedupe="a")
        add_intent(logs, "In flight", value=1, dedupe="b", status="doing",
                   lease={"by": "live-session", "until": time.time() + 600})
        lines = mod.intent_lines()
        assert lines[0].startswith("- doing ") and "In flight" in lines[0]
        assert "Open one" in lines[1]

    def test_an_expired_claim_counts_as_open_again(self, hw):
        mod, logs = hw
        add_intent(logs, "Crashed run", value=4, status="doing",
                   lease={"by": "dead-session", "until": time.time() - 600})
        lines = mod.intent_lines()
        assert len(lines) == 1 and "Crashed run" in lines[0] and "doing" not in lines[0]

    def test_finished_intents_are_not_listed(self, hw):
        mod, logs = hw
        add_intent(logs, "Already finished", value=5, status="done")
        assert mod.intent_lines() == ["(none open)"]

    def test_the_store_file_is_not_modified(self, hw):
        mod, logs = hw
        add_intent(logs, "Leave me alone", value=4, status="doing", lease={"by": "x", "until": time.time() - 5})
        before = (logs / "intents.jsonl").read_bytes()
        mod.intent_lines()
        assert (logs / "intents.jsonl").read_bytes() == before


class TestWrite:
    def test_main_writes_the_handoff_with_no_temp_file_left(self, hw):
        mod, logs = hw
        (logs / "focus.md").write_text("Focus text", encoding="utf-8")
        assert mod.main() == 0
        assert (logs / "handoff.md").exists()
        assert "Focus text" in (logs / "handoff.md").read_text(encoding="utf-8")
        assert sorted(p.name for p in logs.iterdir() if ".tmp" in p.name) == []

    def test_write_atomic_replaces_an_existing_file(self, hw):
        mod, logs = hw
        target = logs / "handoff.md"
        target.write_text("OLD", encoding="utf-8")
        mod.write_atomic(target, "NEW\n")
        assert target.read_text(encoding="utf-8") == "NEW\n"
        assert [p.name for p in logs.iterdir()] == ["handoff.md"]

    def test_a_failed_replace_keeps_the_old_file_and_removes_the_temp(self, hw, monkeypatch):
        mod, logs = hw
        target = logs / "handoff.md"
        target.write_text("OLD", encoding="utf-8")
        def boom(src, dst):
            raise OSError("disk says no")
        monkeypatch.setattr(mod.os, "replace", boom)
        with pytest.raises(OSError):
            mod.write_atomic(target, "NEW\n")
        assert target.read_text(encoding="utf-8") == "OLD"
        assert [p.name for p in logs.iterdir()] == ["handoff.md"]

    def test_main_swallows_a_failed_replace_and_leaves_no_temp(self, hw, monkeypatch):
        mod, logs = hw
        monkeypatch.setattr(mod.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("no")))
        assert mod.main() == 0
        assert list(logs.iterdir()) == []

    def test_utf8_round_trip(self, hw):
        mod, logs = hw
        (logs / "focus.md").write_text("café – naïve 日本語 🎉", encoding="utf-8")
        mod.main()
        assert "café – naïve 日本語 🎉" in (logs / "handoff.md").read_text(encoding="utf-8")

    def test_file_uses_lf_newlines(self, hw):
        mod, logs = hw
        mod.main()
        assert b"\r" not in (logs / "handoff.md").read_bytes()


class TestKillSwitch:
    def test_off_file_means_nothing_is_written(self, hw):
        mod, logs = hw
        (logs / "handoff_off.txt").write_text("", encoding="utf-8")
        assert mod.main() == 0
        assert not (logs / "handoff.md").exists()

    def test_off_file_leaves_an_existing_handoff_untouched(self, hw):
        mod, logs = hw
        (logs / "handoff.md").write_text("KEEP ME", encoding="utf-8")
        (logs / "handoff_off.txt").write_text("", encoding="utf-8")
        mod.main()
        assert (logs / "handoff.md").read_text(encoding="utf-8") == "KEEP ME"

    def test_removing_the_off_file_turns_it_back_on(self, hw):
        mod, logs = hw
        off = logs / "handoff_off.txt"
        off.write_text("", encoding="utf-8")
        mod.main()
        off.unlink()
        mod.main()
        assert (logs / "handoff.md").exists()


class TestMain:
    def test_main_never_raises_when_the_logs_folder_cannot_be_created(self, tmp_path, monkeypatch):
        blocker = tmp_path / "afile"
        blocker.write_text("i am a file, not a folder", encoding="utf-8")
        monkeypatch.setenv("HARNESS_LOGS", str(blocker / "logs"))
        importlib.reload(handoff_write)
        assert handoff_write.main() == 0

    def test_main_never_raises_when_building_explodes(self, hw, monkeypatch):
        mod, logs = hw
        monkeypatch.setattr(mod, "build_handoff", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert mod.main() == 0
        assert not (logs / "handoff.md").exists()

    def test_main_prints_nothing(self, hw, capsys):
        mod, _ = hw
        mod.main()
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""

    def test_running_twice_is_idempotent(self, hw):
        mod, logs = hw
        (logs / "focus.md").write_text("Same focus", encoding="utf-8")
        add_intent(logs, "Same intent", value=4)
        (logs / "alerts.txt").write_text("same alert\n", encoding="utf-8")
        mod.main()
        first = (logs / "handoff.md").read_text(encoding="utf-8")
        mod.main()
        second = (logs / "handoff.md").read_text(encoding="utf-8")
        body = lambda t: t.split("\n", 1)[1]            # the first line holds the write time
        assert body(first) == body(second)
        assert sorted(p.name for p in logs.iterdir()) == ["alerts.txt", "focus.md", "handoff.md", "intents.jsonl"]

    def test_the_handoff_picks_up_changes_on_the_next_run(self, hw):
        mod, logs = hw
        mod.main()
        assert "Brand new focus" not in (logs / "handoff.md").read_text(encoding="utf-8")
        (logs / "focus.md").write_text("Brand new focus", encoding="utf-8")
        mod.main()
        assert "Brand new focus" in (logs / "handoff.md").read_text(encoding="utf-8")


class TestReadStdin:
    def test_valid_utf8_json_bytes(self, monkeypatch):
        stdin_bytes(monkeypatch, '{"stop_hook_active": false, "note": "日本語"}'.encode("utf-8"))
        assert handoff_write.read_stdin() == {"stop_hook_active": False, "note": "日本語"}

    def test_empty_input(self, monkeypatch):
        stdin_bytes(monkeypatch, b"")
        assert handoff_write.read_stdin() == {}

    def test_malformed_json_and_invalid_utf8(self, monkeypatch):
        stdin_bytes(monkeypatch, b"{ not json \xff\xfe")
        assert handoff_write.read_stdin() == {}

    def test_non_object_json(self, monkeypatch):
        stdin_bytes(monkeypatch, b"[1, 2, 3]")
        assert handoff_write.read_stdin() == {}

    def test_a_terminal_is_never_read(self, monkeypatch):
        class Tty:
            def isatty(self):
                return True

            def read(self):
                raise AssertionError("must not block on a terminal")

        monkeypatch.setattr(sys, "stdin", Tty())
        assert handoff_write.read_stdin() == {}


class TestHelpers:
    def test_clip_short_text_is_unchanged(self):
        assert handoff_write.clip("  short  ", 50) == "short"

    def test_clip_never_exceeds_the_limit(self):
        for limit in (3, 10, 40, 200):
            assert len(handoff_write.clip("lorem ipsum dolor " * 100, limit)) <= limit

    def test_one_line_collapses_whitespace(self):
        assert handoff_write.one_line("a\n b\t\tc  ") == "a b c"
        assert handoff_write.one_line(None) == ""


class TestPortability:
    def test_source_has_no_hard_coded_drive_paths(self):
        src = (Path(__file__).parent / "handoff_write.py").read_text(encoding="utf-8")
        assert not re.search(r"[A-Za-z]:[\\/]", src.replace("://", ""))

    def test_runs_as_a_script_end_to_end(self, tmp_path):
        root = tmp_path / "root"
        logs = root / "logs"
        logs.mkdir(parents=True)
        (logs / "focus.md").write_text("Script focus", encoding="utf-8")
        env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(logs), PYTHONIOENCODING="ascii")
        here = Path(__file__).parent
        r = subprocess.run([sys.executable, str(here / "handoff_write.py")], input=b'{"stop_hook_active": false}',
                           env=env, capture_output=True, cwd=str(tmp_path))
        assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
        text = (logs / "handoff.md").read_text(encoding="utf-8")
        assert "Script focus" in text and text.startswith("# Handoff (written ")
