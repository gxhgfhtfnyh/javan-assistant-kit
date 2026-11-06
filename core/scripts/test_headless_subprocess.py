# -*- coding: utf-8 -*-
"""Tests for headless_subprocess.py. The patch is tried on fake Popen classes; one Windows-only test runs a real background (pythonw) process and asks its child whether it got a console."""
import os
import subprocess
import sys
import textwrap
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import headless_subprocess as hs  # noqa: E402


def fake_popen():
    class FakePopen:
        calls = []

        def __init__(self, args, bufsize=-1, executable=None, stdin=None, stdout=None, stderr=None, preexec_fn=None, close_fds=True, shell=False, cwd=None, env=None,
                     universal_newlines=None, startupinfo=None, creationflags=0, **kw):
            FakePopen.calls.append({"args": args, "creationflags": creationflags, "shell": shell})

        def wait(self):
            return 7
    return FakePopen


def no_console():
    return False


def with_console():
    return True


def test_a_background_process_gets_no_window_flag_by_default():
    P = fake_popen()
    assert hs.patch(P, no_console, types.SimpleNamespace(system=lambda c: 0)) is True
    P(["tool"])
    assert P.calls[-1]["creationflags"] == hs.NO_WINDOW == 0x08000000


def test_an_explicit_creationflags_value_is_left_alone_keyword_or_positional():
    P = fake_popen()
    hs.patch(P, no_console, types.SimpleNamespace(system=lambda c: 0))
    P(["tool"], creationflags=0x10)
    assert P.calls[-1]["creationflags"] == 0x10
    P(["tool"], -1, None, None, None, None, None, True, False, None, None, None, None, 0x200)       # creationflags is the 14th positional parameter
    assert P.calls[-1]["creationflags"] == 0x200


def test_a_process_with_a_visible_console_is_not_touched():
    P = fake_popen()
    assert hs.patch(P, with_console, types.SimpleNamespace(system=lambda c: 0)) is False
    P(["tool"])
    assert P.calls[-1]["creationflags"] == 0


def test_patching_twice_wraps_only_once():
    P = fake_popen()
    owner = types.SimpleNamespace(system=lambda c: 0)
    assert hs.patch(P, no_console, owner) is True
    wrapped = P.__init__
    assert hs.patch(P, no_console, owner) is False and P.__init__ is wrapped


def test_os_system_is_routed_through_popen_with_a_shell_and_returns_the_exit_code():
    P = fake_popen()
    owner = types.SimpleNamespace(system=lambda c: 99)
    hs.patch(P, no_console, owner)
    assert owner.system("dir") == 7
    assert P.calls[-1]["shell"] is True and P.calls[-1]["creationflags"] == hs.NO_WINDOW


def test_install_copies_the_file_as_usercustomize_and_is_idempotent(tmp_path):
    assert hs.install(str(tmp_path / "site")) == "installed"
    target = tmp_path / "site" / "usercustomize.py"
    assert target.read_bytes() == open(hs.__file__.replace(".pyc", ".py"), "rb").read()
    assert hs.install(str(tmp_path / "site")) == "already installed"


def test_install_keeps_a_different_existing_usercustomize_and_uninstall_restores_it(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "usercustomize.py").write_text("print('mine')\n", encoding="utf-8")
    assert "kept as" in hs.install(str(site))
    assert (site / "usercustomize.py.bak_headless").read_text(encoding="utf-8") == "print('mine')\n"
    assert "restored" in hs.uninstall(str(site))
    assert (site / "usercustomize.py").read_text(encoding="utf-8") == "print('mine')\n" and not (site / "usercustomize.py.bak_headless").exists()


def test_uninstall_when_nothing_is_installed_and_after_a_plain_install(tmp_path):
    assert hs.uninstall(str(tmp_path)) == "not installed"
    hs.install(str(tmp_path))
    assert hs.uninstall(str(tmp_path)) == "removed" and not (tmp_path / "usercustomize.py").exists()


def test_status_reports_installed_and_active(tmp_path):
    assert "installed: no" in hs.status(str(tmp_path))
    hs.install(str(tmp_path))
    assert "installed: yes" in hs.status(str(tmp_path))
    assert "active in this process" in hs.status(str(tmp_path))


def test_cli_install_status_and_uninstall_use_the_user_site(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hs, "user_site", lambda: str(tmp_path / "us"))
    assert hs.main(["--install"]) == 0 and "installed" in capsys.readouterr().out
    assert hs.main(["--status"]) == 0 and "installed: yes" in capsys.readouterr().out
    assert hs.main(["--uninstall"]) == 0 and "removed" in capsys.readouterr().out
    assert hs.main([]) == 2


def test_importing_the_module_normally_does_not_patch_anything():
    # the patch is applied only when the module is loaded under the name usercustomize
    src = open(hs.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    assert '__name__ == "usercustomize"' in src


@pytest.mark.skipif(os.name != "nt", reason="needs Windows consoles")
def test_a_real_background_process_starts_its_console_child_without_a_window(tmp_path):
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pytest.skip("pythonw.exe not found")
    probe = "import ctypes; open(r'%s', 'w').write(str(ctypes.windll.kernel32.GetConsoleWindow()))"

    def run(patched):
        out = tmp_path / ("out_%s.txt" % patched)
        runner = tmp_path / ("run_%s.py" % patched)
        runner.write_text(textwrap.dedent("""\
            import subprocess, sys
            sys.path.insert(0, r'%s')
            import headless_subprocess as hs
            %s
            subprocess.run([r'%s', '-c', %r], timeout=60)
            """) % (os.path.dirname(hs.__file__), "hs.patch()" if patched else "pass", sys.executable, probe % out), encoding="utf-8")
        subprocess.run([pythonw, str(runner)], timeout=120)
        for _ in range(100):
            if out.exists() and out.read_text():
                break
            import time
            time.sleep(0.2)
        return out.read_text() if out.exists() else None

    control, patched = run(False), run(True)
    assert patched == "0", "the patched background process still gave its child a console window: %r" % patched
    if control in (None, "0"):
        pytest.skip("this environment gives console children no window even without the patch, so the control proves nothing (control=%r)" % control)
    assert control != "0"
