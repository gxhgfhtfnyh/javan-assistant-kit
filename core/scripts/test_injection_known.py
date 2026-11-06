# -*- coding: utf-8 -*-
"""Tests for injection_known.py: signatures, the new-versus-baseline comparison, reading the scan log, the baseline file and the command line."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import injection_known as ik  # noqa: E402


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def logs():
    return Path(os.environ["HARNESS_LOGS"])


def write_scan(*rows):
    with (logs() / "injection_scan.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


PHRASE = {"type": "injection_phrase", "line": 3, "match": "Ignore previous instructions"}
HIDDEN = {"type": "hidden_unicode", "count": 2}
BLOB = {"type": "large_base64_blob", "line": 9, "length": 300}


def test_finding_detail_uses_match_then_count_then_length_then_bytes():
    assert ik.finding_detail(PHRASE) == "ignore previous instructions"
    assert ik.finding_detail(HIDDEN) == "2"
    assert ik.finding_detail(BLOB) == "300"
    assert ik.finding_detail({"type": "oversize", "bytes": 5000000}) == "5000000"
    assert ik.finding_detail({"type": "x"}) == ""


def test_signatures_drop_line_numbers_duplicates_and_malformed_entries():
    sigs = ik.signatures({"a.md": [PHRASE, dict(PHRASE, line=40), HIDDEN, "junk", {"no": "type"}], "b.md": "not a list", "c.md": []})
    assert sigs == {"a.md": [["hidden_unicode", "2"], ["injection_phrase", "ignore previous instructions"]]}


def test_signatures_of_nothing_is_empty():
    assert ik.signatures(None) == {} and ik.signatures({}) == {}


def test_blobs_of_different_length_are_different_findings():
    assert ik.signatures({"a.md": [BLOB]}) != ik.signatures({"a.md": [dict(BLOB, length=999)]})


def test_new_findings_with_no_baseline_is_everything():
    cur = ik.signatures({"a.md": [PHRASE]})
    assert ik.new_findings(cur, None) == cur and ik.new_findings(cur, {}) == cur


def test_new_findings_hides_reviewed_ones_and_keeps_new_ones_even_in_a_known_file():
    base = ik.signatures({"a.md": [PHRASE]})
    cur = ik.signatures({"a.md": [PHRASE, HIDDEN], "b.md": [BLOB]})
    assert ik.new_findings(cur, base) == {"a.md": [["hidden_unicode", "2"]], "b.md": [["large_base64_blob", "300"]]}
    assert ik.new_findings(base, base) == {}


def test_a_line_shift_does_not_make_a_reviewed_finding_new():
    base = ik.signatures({"a.md": [PHRASE]})
    assert ik.new_findings(ik.signatures({"a.md": [dict(PHRASE, line=77)]}), base) == {}


def test_latest_scan_missing_empty_garbage_and_non_dict_rows_give_a_reason_not_a_crash():
    row, why = ik.latest_scan()
    assert row is None and "no scan log" in why
    (logs() / "injection_scan.jsonl").write_text("\n\n", encoding="utf-8")
    assert ik.latest_scan()[0] is None and "empty" in ik.latest_scan()[1]
    (logs() / "injection_scan.jsonl").write_text("{broken\n", encoding="utf-8")
    assert ik.latest_scan()[0] is None and "not valid JSON" in ik.latest_scan()[1]
    (logs() / "injection_scan.jsonl").write_text("[1, 2]\n", encoding="utf-8")
    assert ik.latest_scan()[0] is None and "not a scan result" in ik.latest_scan()[1]


def test_latest_scan_returns_the_last_row(monkeypatch):
    write_scan({"scanned": 1, "findings": {}}, {"scanned": 2, "findings": {"a.md": [PHRASE]}})
    row, why = ik.latest_scan()
    assert why is None and row["scanned"] == 2


def test_latest_scan_reads_only_the_tail_of_a_big_log(monkeypatch):
    monkeypatch.setattr(ik, "TAIL_BYTES", 200)
    old = {"scanned": 1, "findings": {}, "pad": "x" * 500}
    write_scan(old, {"scanned": 7, "findings": {}})
    assert ik.latest_scan()[0]["scanned"] == 7


def test_load_baseline_missing_is_none_without_a_problem_and_corrupt_is_a_problem():
    assert ik.load_baseline() == (None, None)
    (logs() / "injection_baseline.json").write_text("{nope", encoding="utf-8")
    base, why = ik.load_baseline()
    assert base is None and "cannot be read" in why
    (logs() / "injection_baseline.json").write_text("[1]", encoding="utf-8")
    assert "not a mapping" in ik.load_baseline()[1]


def test_save_and_load_baseline_round_trip_and_leave_no_temp_file():
    sigs = ik.signatures({"a.md": [PHRASE, HIDDEN]})
    ik.save_baseline(sigs)
    assert ik.load_baseline() == (sigs, None)
    assert [p.name for p in logs().iterdir() if ".tmp" in p.name] == []


def test_main_without_a_scan_log_exits_2_with_the_reason(capsys):
    assert ik.main([]) == 2
    assert "run injection_scan.py first" in capsys.readouterr().out


def test_main_with_findings_and_no_baseline_reports_them_as_new_and_does_not_write_a_baseline(capsys):
    write_scan({"findings": {"a.md": [PHRASE]}})
    assert ik.main([]) == 1
    assert "NEW injection-scan findings" in capsys.readouterr().out
    assert not (logs() / "injection_baseline.json").exists()


def test_main_accept_then_clean_then_a_new_finding_is_caught(capsys):
    write_scan({"findings": {"a.md": [PHRASE]}})
    assert ik.main(["--accept"]) == 0 and "1 finding" in capsys.readouterr().out
    assert ik.main([]) == 0 and "all reviewed" in capsys.readouterr().out
    write_scan({"findings": {"a.md": [PHRASE], "b.md": [HIDDEN]}})
    assert ik.main([]) == 1
    out = capsys.readouterr().out
    assert "b.md" in out and "a.md" not in out


def test_main_with_no_findings_and_no_baseline_is_clean(capsys):
    write_scan({"findings": {}})
    assert ik.main([]) == 0 and "no injection-scan findings" in capsys.readouterr().out


def test_main_with_a_corrupt_baseline_exits_2_instead_of_hiding_findings(capsys):
    write_scan({"findings": {"a.md": [PHRASE]}})
    (logs() / "injection_baseline.json").write_text("{nope", encoding="utf-8")
    assert ik.main([]) == 2
    assert "cannot be read" in capsys.readouterr().out


def test_the_printed_list_is_cut_to_600_characters(capsys):
    write_scan({"findings": {"n%d.md" % i: [dict(PHRASE, match="phrase %d" % i)] for i in range(100)}})
    assert ik.main([]) == 1
    line = capsys.readouterr().out.strip()
    assert len(line) <= len("NEW injection-scan findings: ") + 600


def test_the_real_process_exit_codes(tmp_path):
    root = tmp_path / "proc"
    (root / "logs").mkdir(parents=True)
    e = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")

    def run(*args):
        return subprocess.run([sys.executable, str(HERE / "injection_known.py"), *args], capture_output=True, text=True, env=e, timeout=30)

    assert run().returncode == 2
    (root / "logs" / "injection_scan.jsonl").write_text(json.dumps({"findings": {"a.md": [PHRASE]}}) + "\n", encoding="utf-8")
    assert run().returncode == 1
    assert run("--accept").returncode == 0
    assert run().returncode == 0
