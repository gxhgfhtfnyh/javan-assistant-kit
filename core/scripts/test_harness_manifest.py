# -*- coding: utf-8 -*-
"""Tests for harness_manifest: the pure core, the readers, and a traced folder on disk. Nothing outside tmp_path is read or written."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness_manifest as hm  # noqa: E402

KNOWN = {"a.py", "b.py", "c.py", "d.py", "orphan.py", "test_a.py", "run.ps1"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake assistant folder; every HARNESS_* variable points into tmp_path."""
    root = tmp_path / "assistant"
    (root / "scripts").mkdir(parents=True)
    (root / ".claude").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------- pure core ----------
def test_script_names_finds_file_mentions_and_python_imports():
    text = 'subprocess.run(["python", "b.py"])\nimport c\nfrom d import thing\nx = "nothing.py"\n'
    assert hm.script_names(text, KNOWN) == {"b.py", "c.py", "d.py"}


def test_script_names_is_case_insensitive_and_ignores_unknown():
    assert hm.script_names('powershell -File "scripts\\RUN.PS1"', KNOWN) == {"run.ps1"}
    assert hm.script_names("import os\nimport sys", KNOWN) == set()
    assert hm.script_names("", KNOWN) == set() and hm.script_names(None, KNOWN) == set()


def test_a_name_inside_a_longer_name_is_not_a_mention():
    known = {"gate.py", "stop_gate.py", "x.py"}
    assert hm.script_names("python scripts/stop_gate.py", known) == {"stop_gate.py"}
    assert hm.script_names("see x.pyc and x.py.bak and data.py", known) == set()


def test_comma_imports_and_dynamic_imports_are_followed():
    text = "import os, b, c as cc\nimport importlib\nm = importlib.import_module('d')\n"
    assert hm.script_names(text, KNOWN) == {"b.py", "c.py", "d.py"}


def test_a_glob_pattern_references_every_file_with_that_prefix():
    known = KNOWN | {"sense_a.py", "sense_b.py", "sensor_x.py"}
    assert hm.script_names('glob.glob(os.path.join(HERE, "sense_*.py"))', known) == {"sense_a.py", "sense_b.py"}


def test_build_graph_never_lets_a_file_reference_itself():
    g = hm.build_graph({"a.py": "import a\nimport b", "b.py": "x = 1"}, KNOWN)
    assert g == {"a.py": {"b.py"}, "b.py": set()}


def test_reach_follows_a_chain_and_survives_cycles():
    g = {"a.py": {"b.py"}, "b.py": {"c.py"}, "c.py": {"a.py"}, "orphan.py": set()}
    assert hm.reach(["a.py"], g) == {"a.py", "b.py", "c.py"}
    assert hm.reach(["orphan.py"], g) == {"orphan.py"}


def test_reach_ignores_entries_that_are_not_files_of_the_graph():
    assert hm.reach(["ghost.py"], {"a.py": set()}) == set()
    assert hm.reach([], {"a.py": set()}) == set()


def test_classify_puts_each_file_in_exactly_one_class_auto_first():
    cls = hm.classify(KNOWN, auto={"a.py", "b.py"}, manual={"b.py", "c.py"})
    assert cls["auto"] == ["a.py", "b.py"]
    assert cls["manual"] == ["c.py"]
    assert cls["test"] == ["test_a.py"]
    assert cls["orphan"] == ["d.py", "orphan.py", "run.ps1"]
    assert sum(len(v) for v in cls.values()) == len(KNOWN)


def test_a_test_file_is_never_counted_as_auto_even_if_reached():
    assert hm.classify({"test_a.py"}, auto={"test_a.py"}, manual=set())["test"] == ["test_a.py"]


# ---------- readers ----------
def test_read_text_caps_the_size_and_survives_a_missing_file(tmp_path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * 5000)
    assert len(hm.read_text(big, cap=100)) == 100
    assert hm.read_text(tmp_path / "nope.txt") == ""


def test_hook_commands_reads_nested_hooks_and_the_status_line(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python scripts/stop_gate.py"}]}],
                                       "PreToolUse": [{"matcher": "*", "hooks": [{"command": "python scripts/send_gate.py"}, "junk", {"command": 5}]}]},
                             "statusLine": {"type": "command", "command": "python scripts/line.py"}}), encoding="utf-8")
    assert sorted(hm.hook_commands([s])) == ["python scripts/line.py", "python scripts/send_gate.py", "python scripts/stop_gate.py"]


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"hooks": []}', '{"hooks": {"Stop": "x"}}', '{"hooks": {"Stop": [5, {"hooks": 3}]}}'])
def test_hook_commands_survives_missing_or_malformed_settings(tmp_path, content):
    s = tmp_path / "settings.json"
    s.write_text(content, encoding="utf-8")
    assert hm.hook_commands([s, tmp_path / "missing.json"]) == []


def test_job_commands_counts_only_enabled_rows(tmp_path):
    (tmp_path / "jobs.json").write_text(json.dumps([
        {"id": "a", "argv": ["python", "scripts/a.py"], "every_min": 5},
        {"id": "b", "argv": ["python", "scripts/b.py"], "enabled": False},
        {"id": "c", "argv": [], "every_min": 5}, "junk"]), encoding="utf-8")
    cmds, n = hm.job_commands(tmp_path)
    assert n == 1 and "a.py" in cmds[0]


def test_job_commands_survives_a_missing_or_broken_table(tmp_path):
    assert hm.job_commands(tmp_path) == ([], 0)
    (tmp_path / "jobs.json").write_text("{oops", encoding="utf-8")
    assert hm.job_commands(tmp_path) == ([], 0)
    (tmp_path / "jobs.json").write_text('{"not": "a list"}', encoding="utf-8")
    assert hm.job_commands(tmp_path) == ([], 0)


def test_scheduler_commands_keeps_only_lines_that_mention_this_folder(home):
    text = "*/5 * * * * cd '%s' && python scripts/job_runner.py --once\n0 3 * * * backup /somewhere/else.py\n" % str(home).replace("\\", "/")
    out = hm.scheduler_commands(lambda: text)
    assert len(out) == 1 and "job_runner.py" in out[0]


def test_scheduler_commands_returns_nothing_when_the_scheduler_is_missing(home):
    def boom():
        raise FileNotFoundError("crontab")
    assert hm.scheduler_commands(boom) == []


# ---------- a folder traced end to end ----------
def build_folder(root):
    s = root / "scripts"
    put(s / "hook_a.py", "import helper\nprint('x')\n")
    put(s / "helper.py", "# helper used by the hook; see deep.py\n")
    put(s / "deep.py", "x = 1\n")
    put(s / "job_runner.py", "# runs the table\n")
    put(s / "nightly.py", "x = 2\n")
    put(s / "off_job.py", "x = 3\n")
    put(s / "manual_tool.py", "import manual_dep\n")
    put(s / "manual_dep.py", "x = 4\n")
    put(s / "lonely.py", "x = 5\n")
    put(s / "test_hook_a.py", "import hook_a\n")
    put(s / "jobs.json", json.dumps([{"id": "n", "argv": ["python", "scripts/nightly.py"], "every_min": 60},
                                     {"id": "o", "argv": ["python", "scripts/off_job.py"], "every_min": 60, "enabled": False}]))
    put(root / ".claude" / "settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "python scripts/hook_a.py"}]}]}}))
    put(root / "CLAUDE.md", "Run `python scripts/manual_tool.py` by hand when asked.\n")


def test_collect_classifies_a_whole_folder(home):
    build_folder(home)
    rep = hm.collect()
    cls = rep["classes"]
    assert cls["auto"] == ["deep.py", "helper.py", "hook_a.py", "job_runner.py", "nightly.py"]
    assert cls["manual"] == ["manual_dep.py", "manual_tool.py"]
    assert cls["test"] == ["test_hook_a.py"]
    assert cls["orphan"] == ["lonely.py", "off_job.py"]
    assert rep["hooks"] == 1 and rep["jobs"] == 1 and rep["files"] == 10


def test_collect_does_not_let_a_test_file_make_its_subject_manual(home):
    build_folder(home)
    put(home / "CLAUDE.md", "Run test_hook_a.py before you commit.\n")
    rep = hm.collect()
    assert "manual_tool.py" in rep["classes"]["orphan"] and "hook_a.py" in rep["classes"]["auto"]


def test_collect_with_a_scheduler_dump_makes_the_job_runner_an_entry(home):
    s = home / "scripts"
    put(s / "job_runner.py", "x = 1\n")
    put(s / "task_target.py", "x = 2\n")
    line = "Task To Run: python %s/scripts/task_target.py" % str(home).replace("\\", "/")
    rep = hm.collect(use_scheduler=True, runner=lambda: line)
    assert rep["classes"]["auto"] == ["task_target.py"] and rep["scheduler_lines"] == 1


def test_collect_on_a_folder_without_scripts_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path / "empty"))
    rep = hm.collect(scripts_dir=tmp_path / "empty" / "scripts")
    assert rep["files"] == 0 and rep["missing_scripts_dir"] is True


def test_render_lists_orphans_only_on_request(home):
    build_folder(home)
    rep = hm.collect()
    assert not any(l.startswith("ORPHAN:") for l in hm.render(rep))
    assert any(l.startswith("ORPHAN:") and "lonely.py" in l for l in hm.render(rep, orphans=True))
    assert any(l.startswith("MANUAL:") and "manual_tool.py" in l for l in hm.render(rep, orphans=True))


def test_main_prints_counts_and_changes_nothing_without_write(home, capsys):
    build_folder(home)
    before = sorted(str(p) for p in home.rglob("*"))
    assert hm.main([]) == 0
    out = capsys.readouterr().out
    assert "files in scripts/: 10" in out and "AUTO" in out and "ORPHAN" in out
    assert sorted(str(p) for p in home.rglob("*")) == before


def test_main_write_saves_the_report_atomically_under_logs(home, capsys):
    build_folder(home)
    assert hm.main(["--write"]) == 0
    saved = json.loads((home / "logs" / "harness_manifest.json").read_text(encoding="utf-8"))
    assert saved["counts"]["auto"] == 5 and saved["counts"]["orphan"] == 2
    assert not list((home / "logs").glob("*.tmp.*"))
    assert "wrote logs/harness_manifest.json" in capsys.readouterr().out


def test_main_json_output_parses(home, capsys):
    build_folder(home)
    hm.main(["--json"])
    assert json.loads(capsys.readouterr().out)["counts"]["test"] == 1


def test_main_with_explicit_settings_and_doc_options(home, tmp_path, capsys):
    build_folder(home)
    other = tmp_path / "other_settings.json"
    other.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "python scripts/lonely.py"}]}]}}), encoding="utf-8")
    note = tmp_path / "note.md"
    note.write_text("use off_job.py now", encoding="utf-8")
    hm.main(["--settings", str(other), "--doc", str(note), "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert "lonely.py" in rep["classes"]["auto"] and "hook_a.py" not in rep["classes"]["auto"]
    assert "off_job.py" in rep["classes"]["manual"]


def test_main_reports_a_missing_scripts_folder_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path / "nothing"))
    assert hm.main(["--scripts-dir", str(tmp_path / "nothing" / "scripts")]) == 0
    assert "scripts folder not found" in capsys.readouterr().out


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    root = tmp_path / "root"
    put(root / "scripts" / "x.py", "x = 1\n")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "harness_manifest.py"), "--orphans"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "ORPHAN nothing references it:" in r.stdout and "ORPHAN: x.py" in r.stdout
