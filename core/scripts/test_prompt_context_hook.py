# -*- coding: utf-8 -*-
"""Tests for prompt_context_hook.py"""

import datetime as dt
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
HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))

import intent_store
import prompt_context_hook

HOOK_PATH = HERE / "prompt_context_hook.py"
TIME_RE = re.compile(r"^\[Current date/time: [A-Z][a-z]+, [A-Z][a-z]+ \d{2}, \d{4}, \d{2}:\d{2} (AM|PM) local time\.\]$")


@pytest.fixture
def hook(tmp_path, monkeypatch):
    """The hook module reloaded against an empty temporary harness root. hook.LOGS is the (existing, empty) logs folder."""
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    importlib.reload(prompt_context_hook)
    return prompt_context_hook


def add_intent(logs, **kw):
    """Append one real intent (built by the kit's own new_intent) to logs/intents.jsonl."""
    kw.setdefault("kind", "report_due")
    kw.setdefault("what", "Do the thing")
    kw.setdefault("tier", 1)
    kw.setdefault("value", 4)
    row = intent_store.new_intent(**kw)
    with open(Path(logs) / "intents.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


class FakeStdin:
    def __init__(self, data: bytes, tty=False):
        self.buffer = io.BytesIO(data)
        self._tty = tty

    def isatty(self):
        return self._tty


# ---------- time line ----------
class TestTimeLine:
    def test_time_line_exact_format(self, hook):
        line = hook.time_line(dt.datetime(2026, 10, 31, 13, 22))
        assert line == "[Current date/time: Saturday, October 31, 2026, 01:22 PM local time.]"

    def test_time_line_morning_and_zero_padding(self, hook):
        line = hook.time_line(dt.datetime(2026, 1, 5, 9, 7))
        assert line == "[Current date/time: Monday, January 05, 2026, 09:07 AM local time.]"

    def test_time_line_default_now_matches_pattern(self, hook):
        assert TIME_RE.match(hook.time_line())


# ---------- focus ----------
class TestFocusLine:
    def test_focus_missing(self, hook):
        assert hook.focus_line() == "[FOCUS file missing: create logs/focus.md]"

    def test_focus_prints_text_under_header(self, hook):
        (hook.LOGS / "focus.md").write_text("CURRENT: write the report\nNEXT: email it\n", encoding="utf-8")
        assert hook.focus_line() == "[FOCUS]\nCURRENT: write the report\nNEXT: email it"

    def test_focus_drops_blank_lines(self, hook):
        (hook.LOGS / "focus.md").write_text("CURRENT: a\n\n\n   \nNEXT: b\n", encoding="utf-8")
        assert hook.focus_line() == "[FOCUS]\nCURRENT: a\nNEXT: b"

    def test_focus_is_capped_at_900_chars_of_text(self, hook):
        (hook.LOGS / "focus.md").write_text("CURRENT: " + "x" * 3000, encoding="utf-8")
        out = hook.focus_line()
        assert out.startswith("[FOCUS]\n")
        assert len(out) == len("[FOCUS]\n") + 900

    def test_rule_lines_move_after_the_real_focus_lines(self, hook):
        (hook.LOGS / "focus.md").write_text("RULE: never skip tests\nCURRENT: ship it\nRULE: be brief\nNEXT: review\n", encoding="utf-8")
        body = hook.focus_line().split("\n")[1:]
        assert body == ["CURRENT: ship it", "NEXT: review", "RULE: never skip tests", "RULE: be brief"]

    def test_the_cap_cuts_rules_not_focus(self, hook):
        long_rule = "RULE: " + "r" * 2000
        (hook.LOGS / "focus.md").write_text(long_rule + "\nCURRENT: the real task\nNEXT: the real next step\nWAIT: nothing\n", encoding="utf-8")
        out = hook.focus_line()
        assert "CURRENT: the real task" in out
        assert "NEXT: the real next step" in out
        assert "WAIT: nothing" in out
        assert out.index("CURRENT:") < out.index("RULE:")
        assert len(out) == len("[FOCUS]\n") + 900

    def test_focus_reads_utf8(self, hook):
        (hook.LOGS / "focus.md").write_text("CURRENT: caf\u00e9 \u2013 na\u00efve \u65e5\u672c\u8a9e\n", encoding="utf-8")
        assert "caf\u00e9 \u2013 na\u00efve \u65e5\u672c\u8a9e" in hook.focus_line()


# ---------- intent line ----------
class TestIntentLine:
    def test_actionable_intent(self, hook):
        row = add_intent(hook.LOGS, what="Do the thing", tier=1, value=4)
        assert hook.intent_line() == "[NEXT INTENT %s tier1 value4] Do the thing  (claim it, do it, then intent_store.py done <id>)" % row["id"]

    def test_highest_value_actionable_intent_wins(self, hook):
        add_intent(hook.LOGS, what="small one", value=2, dedupe="a")
        top = add_intent(hook.LOGS, what="big one", value=5, dedupe="b")
        assert hook.intent_line().startswith("[NEXT INTENT %s tier1 value5] big one" % top["id"])

    def test_done_when_check_is_shown(self, hook):
        add_intent(hook.LOGS, what="Make the file", check={"kind": "exists", "path": "report.txt"})
        assert hook.intent_line().endswith("(claim it, do it, then intent_store.py done <id>; closes only when: report.txt exists)")

    def test_long_what_is_cut_to_150_chars(self, hook):
        add_intent(hook.LOGS, what="w" * 400)
        line = hook.intent_line()
        assert "w" * 150 in line and "w" * 151 not in line

    def test_expired_lease_is_freed_and_shown(self, hook):
        row = add_intent(hook.LOGS, what="stuck run")
        row["status"] = "doing"
        row["lease"] = {"by": "crashed-run", "until": time.time() - 60}
        (hook.LOGS / "intents.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        assert hook.intent_line().startswith("[NEXT INTENT %s tier1 value4] stuck run" % row["id"])

    def test_none_when_queue_is_empty(self, hook):
        assert hook.intent_line() == "[NEXT INTENT] none: nothing actionable: queue empty"

    def test_none_when_intents_file_is_missing_entirely(self, hook):
        assert not (hook.LOGS / "intents.jsonl").exists()
        assert hook.intent_line() == "[NEXT INTENT] none: nothing actionable: queue empty"

    def test_none_explains_a_tier2_item_waiting_on_the_owner(self, hook):
        add_intent(hook.LOGS, what="ask first", tier=2)
        assert hook.intent_line() == "[NEXT INTENT] none: nothing actionable: 1 waiting on the owner"

    def test_none_explains_a_live_lease(self, hook):
        row = add_intent(hook.LOGS, what="being worked")
        row["status"] = "doing"
        row["lease"] = {"by": "live-session", "until": time.time() + 600}
        (hook.LOGS / "intents.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        assert hook.intent_line() == "[NEXT INTENT] none: nothing actionable: 1 already being worked"

    def test_halted_by_loop_halt_file(self, hook):
        add_intent(hook.LOGS)
        (hook.LOGS / "loop_halt.txt").write_text("too many failures in a row\nsecond line ignored\n", encoding="utf-8")
        line = hook.intent_line()
        assert line.startswith("[NEXT INTENT] paused: too many failures in a row  (")
        assert "loop_halt.txt" in line and "second line" not in line
        assert "NEXT INTENT " + "tier" not in line

    def test_halted_by_intents_auto_off_file(self, hook):
        add_intent(hook.LOGS)
        (hook.LOGS / "intents_auto_off.txt").write_text("paused by the owner\n", encoding="utf-8")
        line = hook.intent_line()
        assert line.startswith("[NEXT INTENT] paused: paused by the owner")
        assert "intents_auto_off.txt" in line

    def test_halted_with_an_empty_halt_file_still_says_paused(self, hook):
        (hook.LOGS / "loop_halt.txt").write_text("", encoding="utf-8")
        assert hook.intent_line().startswith("[NEXT INTENT] paused: no reason written")

    def test_unreadable_queue_returns_empty_string_not_an_error(self, hook, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("store broke")
        monkeypatch.setattr(intent_store, "read_jsonl", boom)
        assert hook.intent_line() == ""


# ---------- stdin ----------
class TestReadStdin:
    def test_valid_json_bytes(self, hook, monkeypatch):
        monkeypatch.setattr(sys, "stdin", FakeStdin('{"prompt": "caf\u00e9 \u65e5\u672c"}'.encode("utf-8")))
        assert hook.read_stdin() == {"prompt": "caf\u00e9 \u65e5\u672c"}

    def test_empty_garbage_and_bad_bytes_give_empty_dict(self, hook, monkeypatch):
        for blob in (b"", b"   \n", b"{ not json", b"\xff\xfe\x00bad", b"[1, 2, 3]"):
            monkeypatch.setattr(sys, "stdin", FakeStdin(blob))
            assert hook.read_stdin() == {}

    def test_a_terminal_stdin_is_not_read(self, hook, monkeypatch):
        monkeypatch.setattr(sys, "stdin", FakeStdin(b'{"a": 1}', tty=True))
        assert hook.read_stdin() == {}


# ---------- main ----------
class TestMain:
    def _stdin(self, monkeypatch, blob=b"{}"):
        monkeypatch.setattr(sys, "stdin", FakeStdin(blob))

    def test_main_prints_time_focus_and_intent_in_order(self, hook, monkeypatch, capsys):
        self._stdin(monkeypatch)
        (hook.LOGS / "focus.md").write_text("CURRENT: ship it\n", encoding="utf-8")
        row = add_intent(hook.LOGS, what="Do the thing")
        hook.main()
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 4
        assert TIME_RE.match(lines[0])
        assert lines[1:3] == ["[FOCUS]", "CURRENT: ship it"]
        assert lines[3].startswith("[NEXT INTENT %s tier1 value4] Do the thing" % row["id"])

    def test_main_with_no_focus_and_no_intents(self, hook, monkeypatch, capsys):
        self._stdin(monkeypatch)
        hook.main()
        lines = capsys.readouterr().out.splitlines()
        assert TIME_RE.match(lines[0])
        assert lines[1] == "[FOCUS file missing: create logs/focus.md]"
        assert lines[2] == "[NEXT INTENT] none: nothing actionable: queue empty"

    def test_kill_switch_prints_nothing(self, hook, monkeypatch, capsys):
        self._stdin(monkeypatch)
        (hook.LOGS / "prompt_context_off.txt").write_text("", encoding="utf-8")
        (hook.LOGS / "focus.md").write_text("CURRENT: x\n", encoding="utf-8")
        add_intent(hook.LOGS)
        hook.main()
        assert capsys.readouterr().out == ""

    def test_removing_the_kill_switch_brings_the_output_back(self, hook, monkeypatch, capsys):
        self._stdin(monkeypatch)
        off = hook.LOGS / "prompt_context_off.txt"
        off.write_text("", encoding="utf-8")
        hook.main()
        assert capsys.readouterr().out == ""
        off.unlink()
        self._stdin(monkeypatch)
        hook.main()
        assert "[Current date/time:" in capsys.readouterr().out

    def test_main_never_raises_when_logs_dir_is_missing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path / "no_root"))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "no_such" / "logs"))
        monkeypatch.setattr(sys, "stdin", FakeStdin(b"{}"))
        mod = importlib.reload(prompt_context_hook)
        try:
            mod.main()
        except Exception as e:
            pytest.fail(f"main() raised {e}")
        out = capsys.readouterr().out
        assert "[FOCUS file missing: create logs/focus.md]" in out
        assert "[NEXT INTENT] none:" in out

    def test_main_never_raises_when_logs_is_a_file_and_stdin_is_none(self, tmp_path, monkeypatch, capsys):
        not_a_dir = tmp_path / "logs_is_a_file"
        not_a_dir.write_text("oops", encoding="utf-8")
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(not_a_dir))
        monkeypatch.setattr(sys, "stdin", None)
        try:
            mod = importlib.reload(prompt_context_hook)
            mod.main()
        except Exception as e:
            pytest.fail(f"main() raised {e}")
        assert "[Current date/time:" in capsys.readouterr().out

    def test_main_never_raises_when_a_part_blows_up(self, hook, monkeypatch, capsys):
        self._stdin(monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("part broke")
        monkeypatch.setattr(hook, "focus_line", boom)
        try:
            hook.main()
        except Exception as e:
            pytest.fail(f"main() raised {e}")
        assert "boom" not in capsys.readouterr().out


# ---------- real process, and portability ----------
class TestAsAProcess:
    def _run(self, tmp_path, stdin=b"{}", extra_env=None):
        root = tmp_path / "proc_root"
        (root / "logs").mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"))
        env.update(extra_env or {})
        r = subprocess.run([sys.executable, str(HOOK_PATH)], input=stdin, env=env, capture_output=True, timeout=60)
        return r, root / "logs"

    def test_runs_clean_with_exit_code_zero(self, tmp_path):
        r, logs = self._run(tmp_path)
        assert r.returncode == 0 and r.stderr == b""
        out = r.stdout.decode("utf-8")
        assert out.startswith("[Current date/time:")
        assert "[FOCUS file missing: create logs/focus.md]" in out
        assert "[NEXT INTENT] none: nothing actionable: queue empty" in out

    def test_output_is_utf8_even_when_the_console_encoding_is_ascii(self, tmp_path):
        logs = tmp_path / "proc_root" / "logs"
        logs.mkdir(parents=True)
        (logs / "focus.md").write_text("CURRENT: caf\u00e9 \u2013 na\u00efve\n", encoding="utf-8")
        add_intent(logs, what="caf\u00e9 \u2013 na\u00efve task")
        r, _ = self._run(tmp_path, extra_env={"PYTHONIOENCODING": "ascii"})
        assert r.returncode == 0
        out = r.stdout.decode("utf-8")
        assert "CURRENT: caf\u00e9 \u2013 na\u00efve" in out and "caf\u00e9 \u2013 na\u00efve task" in out

    def test_kill_switch_in_a_real_process(self, tmp_path):
        logs = tmp_path / "proc_root" / "logs"
        logs.mkdir(parents=True)
        (logs / "prompt_context_off.txt").write_text("", encoding="utf-8")
        r, _ = self._run(tmp_path)
        assert r.returncode == 0 and r.stdout == b""

    def test_garbage_stdin_does_not_break_the_output(self, tmp_path):
        r, _ = self._run(tmp_path, stdin=b"\xff\xfe not json at all")
        assert r.returncode == 0 and b"[Current date/time:" in r.stdout

    def test_source_has_no_personal_names_or_hard_coded_drive_paths(self):
        # Words are built from fragments so this test file itself stays clean.
        banned = ["Ar" + "thoven", "Jar" + "vis", "Lo" + "om", "Dis" + "cord", "Omni" + "Route", "presence" + "_last", "situation" + "_line"]
        for f in (HOOK_PATH, Path(__file__)):
            text = f.read_text(encoding="utf-8")
            for word in banned:
                assert word.lower() not in text.lower(), "%s mentions %r" % (f.name, word)
            assert not re.search(r"\b[A-Za-z]:\\", text), "%s has a hard-coded drive path" % f.name
