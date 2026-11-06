# -*- coding: utf-8 -*-
"""Tests for notify.py: the alert line, the optional work item, the optional toast and that it never raises."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import notify  # noqa: E402


def test_format_line_has_stamp_source_and_one_line():
    line = notify.format_line("two\nlines   here", "audit", now=0)
    assert line.startswith("[") and "] audit: two lines here" in line and "\n" not in line


def test_format_line_without_source_and_cut_at_600():
    assert "audit:" not in notify.format_line("x", "", now=0)
    assert len(notify.format_line("y" * 2000, "s", now=0)) == notify.MAX_LINE


def test_alert_appends_to_alerts_txt(tmp_path):
    r = notify.alert("first", "src", logs=tmp_path)
    notify.alert("second", "src", logs=tmp_path)
    lines = (tmp_path / "alerts.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and lines[0].endswith("src: first") and lines[1].endswith("src: second")
    assert r["intent"] is None and r["toast"] is False


def test_alert_with_intent_kind_raises_one_work_item_and_dedupes(tmp_path):
    a = notify.alert("a thing happened", "src", intent_kind="security_alert", tier=2, value=4, dedupe="k1", logs=tmp_path)
    b = notify.alert("a thing happened", "src", intent_kind="security_alert", tier=2, value=4, dedupe="k1", logs=tmp_path)
    assert a["intent"] and b["intent"] is None
    rows = [json.loads(l) for l in (tmp_path / "intents.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1 and rows[0]["kind"] == "security_alert" and rows[0]["tier"] == 2 and rows[0]["value"] == 4
    assert len((tmp_path / "alerts.txt").read_text(encoding="utf-8").splitlines()) == 2        # the log keeps both lines, the queue only one item


def test_toast_needs_both_the_flag_and_the_switch_file(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_toast_for", lambda s, t: calls.append((s, t)) or True)
    assert notify.alert("x", "s", toast_it=True, logs=tmp_path)["toast"] is False            # no switch file
    (tmp_path / "os_toast_on.txt").write_text("on", encoding="utf-8")
    assert notify.alert("x", "s", logs=tmp_path)["toast"] is False                             # switch file but no flag
    assert notify.alert("x", "s", toast=True, logs=tmp_path)["toast"] is True                  # both
    assert calls == [("s", "x")]


def test_toast_never_raises_when_the_notifier_is_missing(monkeypatch):
    def boom(*a, **k):
        raise OSError("no notifier")
    monkeypatch.setattr(notify.subprocess, "Popen", boom)
    assert notify.toast("t", "m") is False


def test_alert_never_raises_when_the_logs_path_is_a_file(tmp_path):
    f = tmp_path / "notafolder"
    f.write_text("x", encoding="utf-8")
    r = notify.alert("x", "s", intent_kind="k", logs=f)
    assert isinstance(r, dict)


def test_cli_prints_the_line_and_writes_the_file(tmp_path):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"))
    r = subprocess.run([sys.executable, str(HERE / "notify.py"), "from the cli", "--source", "cli"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "cli: from the cli" in r.stdout
    assert "cli: from the cli" in (tmp_path / "logs" / "alerts.txt").read_text(encoding="utf-8")
