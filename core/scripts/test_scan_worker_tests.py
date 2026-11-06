# -*- coding: utf-8 -*-
"""Tests for scan_worker_tests.py: what it flags in a test file's source, what it leaves alone, files that cannot be read, and the command line."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scan_worker_tests as sw  # noqa: E402


def msgs(src, **kw):
    return [m for _, m in sw.scan_source(src, **kw)]


def test_a_clean_test_file_has_no_findings():
    src = "import pytest\nimport mod\n\ndef test_x(tmp_path):\n    p = tmp_path / 'a.txt'\n    p.write_text('x')\n    assert mod.f(str(p)) == 1\n"
    assert msgs(src) == []


@pytest.mark.parametrize("src,needle", [
    ("import subprocess\n", "imports subprocess"),
    ("import urllib.request\n", "imports urllib.request"),
    ("from socket import socket\n", "imports socket"),
    ("import pyautogui\n", "imports pyautogui"),
    ("import pyperclip\n", "imports pyperclip"),
    ("import mod\ndef test_a():\n    mod.main()\n", "calls main()"),
    ("import mod\ndef test_a():\n    mod.measure()\n", "calls measure()"),
    ("import mod\ndef test_a():\n    mod.launch()\n", "calls launch()"),
    ("import os\ndef test_a():\n    os.remove('x')\n", "os.remove"),
    ("import os\ndef test_a():\n    os.system('echo')\n", "os.system"),
    ("import shutil\ndef test_a():\n    shutil.rmtree('x')\n", "shutil.rmtree"),
    ("from os import remove\n", "imports os.remove"),
    ("from pathlib import Path\ndef test_a():\n    Path('x').unlink()\n", "Path('x').unlink"),
])
def test_dangerous_things_are_found(src, needle):
    assert any(needle in m for m in msgs(src)), msgs(src)


@pytest.mark.parametrize("path", ["D:\\\\data\\\\out.txt", "D:/data/out.txt", "/var/lib/out.txt", "~/out.txt", "\\\\\\\\server\\\\share\\\\f.txt"])
def test_writing_any_absolute_or_home_path_is_found(path):
    src = "def test_a():\n    open(%r, 'w')\n" % path.replace("\\\\", "\\")
    assert any("writes the absolute or home path" in m for m in msgs(src)), msgs(src)


@pytest.mark.parametrize("src", [
    "def test_a():\n    open('/var/x.txt', mode='a')\n",
    "def test_a():\n    open('/var/x.txt', 'r+')\n",
    "import io\ndef test_a():\n    io.open('/var/x.txt', 'wb')\n",
    "from pathlib import Path\ndef test_a():\n    Path('/var/x.txt').write_text('1')\n",
    "from pathlib import Path\ndef test_a():\n    Path('/var/x.bin').write_bytes(b'1')\n",
    "from pathlib import Path\ndef test_a():\n    Path('/var/x').mkdir()\n",
    "from pathlib import Path\ndef test_a():\n    Path('/var/x.txt').open('w')\n",
    "from pathlib import Path\ndef test_a():\n    (Path.home() / 'notes.txt').write_text('1')\n",
    "import os\ndef test_a():\n    open(os.path.expanduser('~/x'), 'w')\n",
    "import os\ndef test_a():\n    open(os.path.join('/var', 'x'), 'w')\n",
    "def test_a():\n    open(f'/var/{1}.txt', 'w')\n",
    "import os\ndef test_a():\n    os.makedirs('/var/new')\n",
    "import shutil\ndef test_a(tmp_path):\n    shutil.copy(tmp_path / 'a', '/var/b')\n",
])
def test_other_ways_to_write_a_real_place_are_found(src):
    assert msgs(src), src


@pytest.mark.parametrize("src", [
    "def test_a():\n    open('/var/x.txt').read()\n",
    "def test_a():\n    open('/var/x.txt', 'r').read()\n",
    "def test_a(tmp_path):\n    open(tmp_path / 'x', 'w')\n",
    "def test_a(tmp_path):\n    open(str(tmp_path / 'x'), 'w')\n",
    "def test_a():\n    open('relative.txt', 'w')\n",
    "def test_a():\n    open('/dev/null', 'w')\n",
    "from pathlib import Path\ndef test_a(tmp_path):\n    (tmp_path / 'sub').mkdir()\n    Path(tmp_path).joinpath('x')\n",
    "import os\ndef test_a(tmp_path):\n    os.makedirs(tmp_path / 'deep')\n",
    "def test_a(tmp_path):\n    f = tmp_path / 'x'\n    f.write_text('1')\n    f.unlink()\n",
])
def test_reads_relative_paths_dev_null_and_temporary_paths_are_left_alone(src):
    assert msgs(src) == [], msgs(src)


def test_the_finding_names_the_line_number():
    src = "import os\n\n\ndef test_a():\n    os.remove('x')\n"
    assert sw.scan_source(src)[0][0] == 5


def test_pytest_main_in_a_main_guard_is_not_a_finding_but_the_modules_main_is():
    assert msgs("import pytest\nif __name__ == '__main__':\n    pytest.main([__file__, '-q'])\n") == []
    assert any("calls main()" in m for m in msgs("import mod\nif __name__ == '__main__':\n    mod.main()\n"))


def test_extra_calls_extend_the_entry_point_list():
    src = "import mod\ndef test_a():\n    mod.fetch_data()\n"
    assert msgs(src) == []
    assert any("calls fetch_data()" in m for m in msgs(src, extra_calls=["fetch_data"]))


def test_no_function_name_from_a_private_setup_is_built_in():
    assert sw.DANGEROUS_CALLS == {"main", "measure", "launch", "rebaseline", "_append_line"}


def test_a_syntax_error_is_reported_not_raised():
    assert msgs("def f(:\n") == ["test file does not parse"]
    assert msgs("x = '\x00'\n") == ["test file does not parse"]


def test_is_absolute_literal_cases():
    for yes in ("/x", "D:/x", "D:\\x", "\\\\srv\\x", "~", "~/x", "~\\x"):
        assert sw.is_absolute_literal(yes), yes
    for no in ("", "x", "./x", "rel/x", "~user", "http://x", None, 5, "/dev/null"):
        assert not sw.is_absolute_literal(no), no


# ---- files and the command line
def test_scan_file_reads_a_file_and_reports_a_missing_or_oversize_one(tmp_path, monkeypatch):
    f = tmp_path / "test_x.py"
    f.write_text("import subprocess\n", encoding="utf-8")
    assert sw.scan_file(f) == ([(1, "imports subprocess")], None)
    assert sw.scan_file(tmp_path / "nope.py")[1].startswith("cannot read")
    monkeypatch.setattr(sw, "MAX_BYTES", 5)
    assert "not read" in sw.scan_file(f)[1]


def test_scan_file_survives_undecodable_bytes(tmp_path):
    f = tmp_path / "test_x.py"
    f.write_bytes(b"x = '\xff\xfe'\nimport socket\n")
    assert [m for _l, m in sw.scan_file(f)[0]] == ["imports socket"]


def test_expand_handles_globs_and_keeps_unmatched_names(tmp_path):
    (tmp_path / "test_a.py").write_text("", encoding="utf-8")
    (tmp_path / "test_b.py").write_text("", encoding="utf-8")
    got = sw.expand([str(tmp_path / "test_*.py"), str(tmp_path / "gone_*.py"), "plain.py"])
    assert [os.path.basename(g) for g in got] == ["test_a.py", "test_b.py", "gone_*.py", "plain.py"]


def test_main_exit_codes(tmp_path, capsys):
    clean = tmp_path / "test_clean.py"
    clean.write_text("def test_a(tmp_path):\n    assert 1\n", encoding="utf-8")
    dirty = tmp_path / "test_dirty.py"
    dirty.write_text("import subprocess\n", encoding="utf-8")
    assert sw.main([str(clean)]) == 0 and "0 finding(s) in 1 file(s)" in capsys.readouterr().out
    assert sw.main([str(clean), str(dirty)]) == 1
    out = capsys.readouterr().out
    assert "test_dirty.py:1 imports subprocess" in out and "1 finding(s) in 2 file(s)" in out
    assert sw.main([str(tmp_path / "missing.py")]) == 2 and "cannot read" in capsys.readouterr().out
    assert sw.main([str(tmp_path / "missing.py"), str(dirty)]) == 1
    assert sw.main([]) == 2 and "Usage" in capsys.readouterr().out


def test_main_extra_calls_option(tmp_path, capsys):
    f = tmp_path / "test_x.py"
    f.write_text("import mod\ndef test_a():\n    mod.go_live()\n", encoding="utf-8")
    assert sw.main([str(f)]) == 0
    assert sw.main([str(f), "--extra-calls", "go_live, other"]) == 1


def test_scanning_never_runs_the_file(tmp_path):
    marker = tmp_path / "ran.txt"
    f = tmp_path / "test_x.py"
    f.write_text("open(%r, 'w').write('ran')\n" % str(marker), encoding="utf-8")
    sw.scan_file(f)
    assert not marker.exists()


def test_the_real_process_prints_findings_and_exits_1(tmp_path):
    f = tmp_path / "test_x.py"
    f.write_text("import socket\n", encoding="utf-8")
    e = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "scan_worker_tests.py"), str(f)], capture_output=True, text=True, env=e, timeout=30)
    assert r.returncode == 1 and "imports socket" in r.stdout
