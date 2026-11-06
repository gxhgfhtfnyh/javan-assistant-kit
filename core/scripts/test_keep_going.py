# -*- coding: utf-8 -*-
"""Tests for keep_going.py"""

import importlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Add the scripts directory to path
HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))

import keep_going


@pytest.fixture
def kg(tmp_path, monkeypatch):
    """keep_going reloaded so every path points into a temp HARNESS_ROOT."""
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.delenv("HARNESS_LOGS", raising=False)
    importlib.reload(keep_going)
    return keep_going


def assistant_row(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def write_transcript(path, texts, junk=()):
    rows = [json.dumps(assistant_row(t)) for t in texts]
    rows.extend(junk)
    Path(path).write_text("\n".join(rows) + "\n", encoding="utf-8")
    return str(path)


def run_main(kg, monkeypatch, payload):
    """Run kg.main() with payload (dict, str or bytes) on stdin."""
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8"))
    kg.main()


def log_rows(kg):
    if not kg.LOG.exists():
        return []
    return [json.loads(line) for line in kg.LOG.read_text(encoding="utf-8").splitlines() if line.strip()]


PROMISES = [
    "Tests pass. Next I'm going to build the parser.",
    "Done with part one. I'll now wire up the config loader.",
    "Now I'm checking the other files.",
    "I'm going to write the docs after this.",
    "Next step is to run the migration.",
    "Going to fix the import after this.",
    "Next up: the tests.",
    "I'm starting the build.",
]

NO_PROMISE = [
    "The build finished and all 12 tests pass.",
    "I fixed the bug in parse.py.",
    "How I'm going to explain this is simple: it is a cache.",
    "Here is the summary of changes.",
]

WAITING_PROMISES = [
    "Next I'm going to deploy, but I'm waiting on you to approve.",
    "I'll now start. Say the word.",
    "Next I'm going to run it once you send the file.",
    "I'm going to build it if you want.",
    "I'm going to start after this. Your call.",
    "Next up: the migration. I need you to confirm the database name.",
    "Next I'm doing the review. Tell me if the scope changes.",
    "I'm about to start, waiting for your answer.",
    "I'll now continue as soon as that is true, but until you reply I stay put.",
]


class TestPromiseDetection:
    @pytest.mark.parametrize("text", PROMISES)
    def test_promise_blocks(self, text):
        block, why = keep_going.should_block(text)
        assert block is True
        assert why.startswith("promised '")
        assert "and stopped" in why

    @pytest.mark.parametrize("text", NO_PROMISE)
    def test_no_promise_allows(self, text):
        assert keep_going.should_block(text) == (False, "no promise")

    @pytest.mark.parametrize("text", WAITING_PROMISES)
    def test_waiting_phrase_allows(self, text):
        assert keep_going.should_block(text) == (False, "waiting on the user")

    def test_curly_apostrophe_i_m_is_detected(self):
        block, why = keep_going.should_block("Done. Next I’m going to build the index.")
        assert block is True
        assert "'" in why and "’" not in why

    def test_curly_apostrophe_i_ll_now_is_detected(self):
        assert keep_going.should_block("Saved. I’ll now write the tests.")[0] is True

    def test_left_curly_apostrophe_is_detected(self):
        assert keep_going.should_block("Saved. I‘ll now write the tests.")[0] is True

    def test_curly_apostrophe_waiting_phrase_still_allows(self):
        assert keep_going.should_block("I’m going to start. Tell me if you want it changed.") == (False, "waiting on the user")

    def test_promise_is_case_insensitive(self):
        assert keep_going.should_block("NEXT I'M GOING TO BUILD IT")[0] is True

    def test_only_the_last_500_chars_count_for_promises(self):
        text = "Next I'm going to build it. " + "x" * 600
        assert keep_going.should_block(text) == (False, "no promise")

    def test_a_waiting_phrase_outside_the_tail_does_not_excuse_a_promise(self):
        text = "I'm waiting on you. " + "y" * 600 + " Next I'm going to build it."
        assert keep_going.should_block(text)[0] is True

    def test_empty_and_none_text_allow(self):
        assert keep_going.should_block("") == (False, "no text")
        assert keep_going.should_block("   \n ") == (False, "no text")
        assert keep_going.should_block(None) == (False, "no text")


class TestLoopGuard:
    def test_stop_hook_active_allows_a_promise(self):
        assert keep_going.should_block("Next I'm going to build it.", stop_hook_active=True) == (False, "already continuing")

    def test_stop_hook_active_allows_an_open_queue(self):
        assert keep_going.should_block("done", stop_hook_active=True, queue_open=["a"]) == (False, "already continuing")

    def test_main_allows_when_stop_hook_active(self, kg, monkeypatch, capsys, tmp_path):
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build it."])
        run_main(kg, monkeypatch, {"transcript_path": tr, "stop_hook_active": True})
        assert capsys.readouterr().out == ""
        rows = log_rows(kg)
        assert rows[-1]["block"] is False and rows[-1]["why"] == "already continuing"


class TestShouldBlockQueue:
    def test_open_queue_blocks_a_closing_message_without_a_promise(self):
        block, why = keep_going.should_block("All finished.", queue_open=["write the changelog"])
        assert block is True
        assert why == "queue still has 1 open item(s): write the changelog"

    def test_open_queue_blocks_even_when_the_message_says_nothing_is_left(self):
        assert keep_going.should_block("Nothing else left, all done.", queue_open=["write the changelog"])[0] is True

    def test_queue_reason_shows_two_items_and_the_count(self):
        block, why = keep_going.should_block("ok", queue_open=["one", "two", "three"])
        assert block is True
        assert "3 open item(s)" in why and "one; two" in why and "three" not in why

    def test_queue_reason_truncates_long_items(self):
        block, why = keep_going.should_block("ok", queue_open=["z" * 200])
        assert "z" * 70 in why and "z" * 71 not in why

    def test_empty_queue_list_does_not_block(self):
        assert keep_going.should_block("ok", queue_open=[]) == (False, "no promise")

    def test_promise_wins_over_queue_in_the_reason(self):
        block, why = keep_going.should_block("Next I'm going to build it.", queue_open=["a"])
        assert block is True and why.startswith("promised")

    def test_waiting_on_job_does_not_cause_a_block(self):
        assert keep_going.should_block("ok", waiting_on_job=True) == (False, "waiting on a background job")

    def test_waiting_on_job_does_not_excuse_a_promise(self):
        assert keep_going.should_block("Next I'm going to build it.", waiting_on_job=True)[0] is True

    def test_waiting_on_job_does_not_excuse_an_open_queue(self):
        assert keep_going.should_block("ok", queue_open=["a"], waiting_on_job=True)[0] is True


class TestReadQueue:
    def test_missing_file_is_empty(self, kg):
        assert kg.read_queue_open() == []
        assert kg.notifier_pending() is False

    def test_open_done_and_plain_waiting(self, kg):
        kg.QUEUE.write_text(
            "# Queue\n"
            "- [ ] write the changelog\n"
            "- [x] fix the parser\n"
            "- [~] get the key (waiting: user must paste it)\n"
            "- [ ]   trim me   \n"
            "some other line\n",
            encoding="utf-8",
        )
        assert kg.read_queue_open() == ["write the changelog", "trim me"]

    def test_indented_open_item_counts(self, kg):
        kg.QUEUE.write_text("  - [ ] nested item\n", encoding="utf-8")
        assert kg.read_queue_open() == ["nested item"]

    def test_uppercase_x_is_not_open(self, kg):
        kg.QUEUE.write_text("- [X] done item\n", encoding="utf-8")
        assert kg.read_queue_open() == []

    def test_background_waiting_without_task_id_counts_open(self, kg):
        kg.QUEUE.write_text("- [~] run the benchmark (waiting: background job)\n", encoding="utf-8")
        out = kg.read_queue_open()
        assert len(out) == 1
        assert out[0].startswith("[no notifying task named]") and "run the benchmark" in out[0]
        assert kg.notifier_pending() is False

    def test_background_waiting_with_task_id_is_not_open(self, kg):
        kg.QUEUE.write_text("- [~] run the benchmark (waiting: background job b8x3k9d2f)\n", encoding="utf-8")
        assert kg.read_queue_open() == []
        assert kg.notifier_pending() is True

    def test_plain_words_are_not_mistaken_for_task_ids(self, kg):
        kg.QUEUE.write_text("- [~] run it (waiting: background benchmark)\n", encoding="utf-8")
        assert len(kg.read_queue_open()) == 1
        assert kg.notifier_pending() is False

    def test_waiting_without_background_words_never_counts(self, kg):
        kg.QUEUE.write_text("- [~] pick a name (waiting: user decision)\n- [~] no reason given\n", encoding="utf-8")
        assert kg.read_queue_open() == []

    def test_unreadable_bytes_do_not_crash(self, kg):
        kg.QUEUE.write_bytes(b"- [ ] caf\xe9 item\n- [ ] ok\n")
        out = kg.read_queue_open()
        assert len(out) == 2 and out[1] == "ok"

    def test_explicit_path_argument(self, tmp_path, kg):
        p = tmp_path / "other.md"
        p.write_text("- [ ] elsewhere\n", encoding="utf-8")
        assert kg.read_queue_open(p) == ["elsewhere"]


class TestMainBlocking:
    def test_promise_blocks_with_the_documented_json_shape(self, kg, monkeypatch, capsys, tmp_path):
        tr = write_transcript(tmp_path / "t.jsonl", ["Tests pass. Next I'm going to build the parser."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        out = capsys.readouterr().out
        assert out != ""
        data = json.loads(out)
        assert set(data) == {"decision", "reason"}
        assert data["decision"] == "block"
        assert "promising a next step" in data["reason"]
        assert "tool call" in data["reason"]
        assert "the user" in data["reason"]
        assert out.isascii()

    def test_open_queue_blocks_and_names_the_queue_file(self, kg, monkeypatch, capsys, tmp_path):
        kg.QUEUE.write_text("- [ ] write the changelog\n", encoding="utf-8")
        tr = write_transcript(tmp_path / "t.jsonl", ["All finished."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        data = json.loads(capsys.readouterr().out)
        assert data["decision"] == "block"
        assert "write the changelog" in data["reason"]
        assert "queue.md" in data["reason"]
        assert "- [x]" in data["reason"] and "- [~]" in data["reason"]

    def test_done_and_waiting_queue_lets_the_stop_through(self, kg, monkeypatch, capsys, tmp_path):
        kg.QUEUE.write_text("- [x] done\n- [~] approval (waiting: user)\n", encoding="utf-8")
        tr = write_transcript(tmp_path / "t.jsonl", ["All finished."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert capsys.readouterr().out == ""

    def test_plain_closing_message_allows(self, kg, monkeypatch, capsys, tmp_path):
        tr = write_transcript(tmp_path / "t.jsonl", ["Fixed it. Anything else?"])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert capsys.readouterr().out == ""
        assert log_rows(kg)[-1]["why"] == "no promise"

    def test_waiting_promise_allows(self, kg, monkeypatch, capsys, tmp_path):
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to deploy, once you send the key."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert capsys.readouterr().out == ""

    def test_only_the_last_assistant_message_counts(self, kg, monkeypatch, capsys, tmp_path):
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build it.", "Built it. All done."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert capsys.readouterr().out == ""

    def test_decision_is_logged(self, kg, monkeypatch, capsys, tmp_path):
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build the parser."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        capsys.readouterr()
        row = log_rows(kg)[-1]
        assert row["block"] is True
        assert row["why"].startswith("promised")
        assert "build the parser" in row["tail"]
        assert set(row) == {"ts", "block", "why", "tail"}

    def test_utf8_bytes_and_bom_on_stdin(self, kg, monkeypatch, capsys, tmp_path):
        tr = write_transcript(tmp_path / "t.jsonl", ["Café done. Next I'm going to build the café index — now."])
        payload = b"\xef\xbb\xbf" + json.dumps({"transcript_path": tr}, ensure_ascii=False).encode("utf-8")
        run_main(kg, monkeypatch, payload)
        data = json.loads(capsys.readouterr().out)
        assert data["decision"] == "block"


class TestHourlyCap:
    def test_blocks_are_recorded_in_state(self, kg, monkeypatch, capsys, tmp_path):
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build it."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        run_main(kg, monkeypatch, {"transcript_path": tr})
        capsys.readouterr()
        state = json.loads(kg.STATE.read_text(encoding="utf-8"))
        assert len(state["blocks"]) == 2

    def test_cap_of_six_per_hour_allows_the_seventh(self, kg, monkeypatch, capsys, tmp_path):
        assert kg.MAX_BLOCKS_PER_HOUR == 6
        now = time.time()
        kg.STATE.write_text(json.dumps({"blocks": [now - 10 * i for i in range(1, 7)]}), encoding="utf-8")
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build it."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert capsys.readouterr().out == ""
        row = log_rows(kg)[-1]
        assert row["block"] is False and row["why"] == "hourly cap reached"
        assert len(json.loads(kg.STATE.read_text(encoding="utf-8"))["blocks"]) == 6

    def test_five_recent_blocks_still_block_the_sixth(self, kg, monkeypatch, capsys, tmp_path):
        now = time.time()
        kg.STATE.write_text(json.dumps({"blocks": [now - 10 * i for i in range(1, 6)]}), encoding="utf-8")
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build it."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert json.loads(capsys.readouterr().out)["decision"] == "block"
        assert len(json.loads(kg.STATE.read_text(encoding="utf-8"))["blocks"]) == 6

    def test_blocks_older_than_an_hour_do_not_count_and_are_dropped(self, kg, monkeypatch, capsys, tmp_path):
        now = time.time()
        kg.STATE.write_text(json.dumps({"blocks": [now - 4000 - i for i in range(8)]}), encoding="utf-8")
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build it."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert json.loads(capsys.readouterr().out)["decision"] == "block"
        assert len(json.loads(kg.STATE.read_text(encoding="utf-8"))["blocks"]) == 1

    def test_cap_also_covers_queue_blocks(self, kg, monkeypatch, capsys, tmp_path):
        now = time.time()
        kg.STATE.write_text(json.dumps({"blocks": [now - 10 * i for i in range(1, 7)]}), encoding="utf-8")
        kg.QUEUE.write_text("- [ ] still open\n", encoding="utf-8")
        tr = write_transcript(tmp_path / "t.jsonl", ["All finished."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert capsys.readouterr().out == ""

    def test_blocks_this_hour_helper(self, kg):
        now = time.time()
        assert kg.blocks_this_hour({}, now) == []
        assert len(kg.blocks_this_hour({"blocks": [now - 100, now - 3700, now - 200]}, now)) == 2

    def test_garbage_in_state_is_ignored(self, kg):
        now = time.time()
        assert kg.blocks_this_hour({"blocks": "oops"}, now) == []
        assert kg.blocks_this_hour({"blocks": [None, "x", True, now - 5]}, now) == [now - 5]

    def test_malformed_state_file_is_treated_as_empty(self, kg, monkeypatch, capsys, tmp_path):
        kg.STATE.write_text("not json", encoding="utf-8")
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build it."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert json.loads(capsys.readouterr().out)["decision"] == "block"
        assert len(json.loads(kg.STATE.read_text(encoding="utf-8"))["blocks"]) == 1

    def test_record_block_prunes_and_appends(self, kg):
        now = time.time()
        out = kg.record_block({"blocks": [now - 5000, now - 10]}, now)
        assert out == {"blocks": [now - 10, now]}


class TestOffSwitch:
    def test_off_file_allows_and_logs_nothing(self, kg, monkeypatch, capsys, tmp_path):
        (kg.LOGS / "keep_going_off.txt").write_text("", encoding="utf-8")
        kg.QUEUE.write_text("- [ ] open item\n", encoding="utf-8")
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build it."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert capsys.readouterr().out == ""
        assert not kg.LOG.exists() and not kg.STATE.exists()

    def test_removing_the_off_file_resumes_blocking(self, kg, monkeypatch, capsys, tmp_path):
        off = kg.LOGS / "keep_going_off.txt"
        off.write_text("", encoding="utf-8")
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build it."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert capsys.readouterr().out == ""
        off.unlink()
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert json.loads(capsys.readouterr().out)["decision"] == "block"


class TestTranscript:
    def test_garbage_lines_between_valid_rows_are_skipped(self, kg, monkeypatch, capsys, tmp_path):
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build it."], junk=["{ not json", "", "[1,2", "null", "42"])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert json.loads(capsys.readouterr().out)["decision"] == "block"

    def test_fully_malformed_transcript_allows(self, kg, monkeypatch, capsys, tmp_path):
        p = tmp_path / "bad.jsonl"
        p.write_bytes(b"\xff\xfe\x00garbage\n{{{{\nnot json at all\n")
        run_main(kg, monkeypatch, {"transcript_path": str(p)})
        assert capsys.readouterr().out == ""
        assert log_rows(kg)[-1]["why"] == "no text"

    def test_missing_transcript_file_allows(self, kg, monkeypatch, capsys, tmp_path):
        run_main(kg, monkeypatch, {"transcript_path": str(tmp_path / "nope.jsonl")})
        assert capsys.readouterr().out == ""

    def test_empty_transcript_allows(self, kg, monkeypatch, capsys, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        run_main(kg, monkeypatch, {"transcript_path": str(p)})
        assert capsys.readouterr().out == ""

    def test_no_transcript_path_allows(self, kg, monkeypatch, capsys):
        run_main(kg, monkeypatch, {"stop_hook_active": False})
        assert capsys.readouterr().out == ""

    def test_non_string_transcript_path_allows(self, kg, monkeypatch, capsys):
        run_main(kg, monkeypatch, {"transcript_path": 12345})
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("payload", ["", "   \n", "{ not json", "[1, 2, 3]", "null", b"\xff\xfe\x00\x01"])
    def test_malformed_stdin_allows(self, kg, monkeypatch, capsys, payload):
        run_main(kg, monkeypatch, payload)
        assert capsys.readouterr().out == ""

    def test_last_assistant_text_picks_the_last_text_block(self, kg):
        rows = [
            assistant_row("first"),
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}, {"type": "text", "text": "second"}, {"type": "tool_use", "name": "Read"}]}},
        ]
        assert kg.last_assistant_text(rows) == "second"

    def test_last_assistant_text_accepts_string_content_and_odd_rows(self, kg):
        rows = ["junk", None, {"type": "assistant"}, {"type": "assistant", "message": "x"},
                {"type": "assistant", "message": {"content": [None, 3, {"type": "text", "text": None}]}},
                {"type": "assistant", "message": {"content": "plain text"}}]
        assert kg.last_assistant_text(rows) == "plain text"

    def test_last_assistant_text_ignores_blank_text(self, kg):
        assert kg.last_assistant_text([assistant_row("real"), assistant_row("   ")]) == "real"

    def test_tail_lines_limits_and_drops_a_cut_first_line(self, kg, tmp_path):
        p = tmp_path / "big.jsonl"
        lines = [json.dumps({"n": i, "pad": "p" * 40}) for i in range(200)]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        got = kg.tail_lines(p, max_lines=1000, max_bytes=500)
        assert 0 < len(got) < 20
        assert all(json.loads(g)["n"] >= 180 for g in got)       # every line is whole JSON, so the cut line was dropped
        assert json.loads(got[-1])["n"] == 199
        assert len(kg.tail_lines(p, max_lines=5)) == 5


class TestNeverRaises:
    def test_unwritable_logs_dir_still_blocks_and_does_not_raise(self, tmp_path, monkeypatch, capsys):
        not_a_dir = tmp_path / "logs_is_a_file"
        not_a_dir.write_text("x", encoding="utf-8")
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(not_a_dir))
        importlib.reload(keep_going)
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build it."])
        run_main(keep_going, monkeypatch, {"transcript_path": tr})
        assert json.loads(capsys.readouterr().out)["decision"] == "block"

    def test_stdin_that_raises_does_not_raise(self, kg, monkeypatch, capsys):
        class Boom:
            def read(self):
                raise RuntimeError("boom")
        monkeypatch.setattr(sys, "stdin", Boom())
        kg.main()
        assert capsys.readouterr().out == ""

    def test_should_block_exception_is_swallowed(self, kg, monkeypatch, capsys, tmp_path):
        def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(kg, "should_block", boom)
        tr = write_transcript(tmp_path / "t.jsonl", ["Next I'm going to build it."])
        run_main(kg, monkeypatch, {"transcript_path": tr})
        assert capsys.readouterr().out == ""


class TestPathsAndConstants:
    def test_paths_follow_harness_root(self, kg, tmp_path):
        logs = (tmp_path / "root" / "logs").resolve()
        assert kg.LOGS == logs
        assert kg.LOG == logs / "keep_going.jsonl"
        assert kg.STATE == logs / "keep_going_state.json"
        assert kg.OFF == logs / "keep_going_off.txt"
        assert kg.QUEUE == logs / "queue.md"

    def test_harness_logs_overrides_the_logs_dir(self, tmp_path, monkeypatch):
        other = tmp_path / "elsewhere"
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path / "root"))
        monkeypatch.setenv("HARNESS_LOGS", str(other))
        importlib.reload(keep_going)
        assert keep_going.LOGS == other.resolve()
        assert keep_going.QUEUE == other.resolve() / "queue.md"

    def test_build_message_variants(self, kg):
        q = kg.build_message("queue still has 1 open item(s): x")
        assert "work queue" in q and "queue.md" in q
        p = kg.build_message("promised 'Next I'm' and stopped")
        assert "promising a next step" in p and "the user" in p


class TestSubprocess:
    def test_runs_as_a_script_with_utf8_stdin_and_ascii_stdout(self, tmp_path):
        root = tmp_path / "root"
        (root / "logs").mkdir(parents=True)
        tr = write_transcript(tmp_path / "t.jsonl", ["Café — done. Next I'm going to build the café index."])
        payload = json.dumps({"transcript_path": tr}, ensure_ascii=False).encode("utf-8")
        env = dict(os.environ, HARNESS_ROOT=str(root), PYTHONIOENCODING="ascii")
        env.pop("HARNESS_LOGS", None)
        r = subprocess.run([sys.executable, str(HERE / "keep_going.py")], input=payload, env=env, capture_output=True)
        assert r.returncode == 0
        out = r.stdout.decode("ascii")
        assert json.loads(out)["decision"] == "block"
        assert (root / "logs" / "keep_going.jsonl").exists()
        assert (root / "logs" / "keep_going_state.json").exists()

    def test_script_prints_nothing_when_there_is_no_promise(self, tmp_path):
        root = tmp_path / "root"
        (root / "logs").mkdir(parents=True)
        tr = write_transcript(tmp_path / "t.jsonl", ["All done, tests pass."])
        env = dict(os.environ, HARNESS_ROOT=str(root))
        env.pop("HARNESS_LOGS", None)
        r = subprocess.run([sys.executable, str(HERE / "keep_going.py")], input=json.dumps({"transcript_path": tr}).encode("utf-8"), env=env, capture_output=True)
        assert r.returncode == 0 and r.stdout == b""
