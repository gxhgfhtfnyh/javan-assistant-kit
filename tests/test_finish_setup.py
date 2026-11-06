"""Tests for finish_setup.py: nothing real is installed, registered or launched; the runner, the opener and the launcher are fakes that record what would have been done."""
import json
import os
import shlex
import stat
import subprocess
import sys

import pytest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)
import finish_setup as fs  # noqa: E402


class Fake:
    def __init__(self, script=None):
        self.calls = []
        self.script = script or {}

    def __call__(self, args, timeout=900):
        self.calls.append(args)
        key = args if isinstance(args, str) else " ".join(args)
        for needle, reply in self.script.items():
            if needle in key:
                return reply
        return 0, ""


class Launcher:
    def __init__(self, ok=True, detail="started"):
        self.calls, self.ok, self.detail = [], ok, detail

    def __call__(self, argv):
        self.calls.append(argv)
        return self.ok, self.detail


def ctx(tmp_path, platform="win32", runner=None, which=None, opener=None, home=None, launcher=None, env=None, name="assistant"):
    which = which or (lambda name: None if name == "obsidian" else name)
    out = tmp_path / name
    (out / "scripts").mkdir(parents=True, exist_ok=True)
    return fs.Context(str(out), platform=platform, runner=runner or Fake(), home=str(home or tmp_path / "home"), opener=opener or (lambda target: None),
                      which=which, python="python", launcher=launcher or Launcher(), env={} if env is None else env)


def with_obsidian(tmp_path, where=("Programs", "Obsidian")):
    local = tmp_path / "local"
    folder = local.joinpath(*where)
    folder.mkdir(parents=True)
    (folder / "Obsidian.exe").write_text("")
    return {"LOCALAPPDATA": str(local)}


# ---------- pytest / headless / scheduler ----------
def test_pytest_is_left_alone_when_present_and_installed_when_missing(tmp_path):
    present = Fake()
    assert fs.step_pytest(ctx(tmp_path, runner=present))["status"] == "skipped" and len(present.calls) == 1
    missing = Fake({"import pytest": (1, "no module"), "pip install": (0, "")})
    r = fs.step_pytest(ctx(tmp_path, runner=missing))
    assert r["status"] == "done" and missing.calls[-1] == ["python", "-m", "pip", "install", "pytest"]


def test_a_locked_system_python_falls_back_to_a_user_install_and_then_to_the_flag_pip_asks_for(tmp_path):
    locked = Fake({"import pytest": (1, ""), "pip install pytest": (1, "error: externally-managed-environment"), "--user pytest": (1, "error: externally-managed-environment")})
    locked.script["--break-system-packages"] = (0, "")
    r = fs.step_pytest(ctx(tmp_path, runner=locked, platform="linux"))
    assert r["status"] == "done"
    assert [c[3:] for c in locked.calls[1:]] == [["install", "pytest"], ["install", "--user", "pytest"], ["install", "--user", "--break-system-packages", "pytest"]]


def test_the_user_install_is_enough_when_it_works(tmp_path):
    f = Fake({"import pytest": (1, ""), "install pytest": (1, "denied"), "--user pytest": (0, "")})
    r = fs.step_pytest(ctx(tmp_path, runner=f))
    assert r["status"] == "done" and not any("--break-system-packages" in c for c in f.calls)


def test_a_failed_pip_install_is_a_failure_with_the_reasons(tmp_path):
    f = Fake({"import pytest": (1, ""), "pip install": (1, "no network")})
    r = fs.step_pytest(ctx(tmp_path, runner=f))
    assert r["status"] == "failed" and "no network" in r["detail"] and not any("--break-system-packages" in c for c in f.calls)


def test_the_headless_patch_is_only_installed_on_windows(tmp_path):
    f = Fake()
    assert fs.step_headless(ctx(tmp_path, platform="linux", runner=f))["status"] == "skipped" and f.calls == []
    r = fs.step_headless(ctx(tmp_path, runner=f))
    assert r["status"] == "done" and f.calls[0][-1] == "--install" and f.calls[0][1].endswith("headless_subprocess.py")


def test_the_scheduler_is_registered_with_apply_and_each_exit_code_has_its_status(tmp_path):
    f = Fake({"install_scheduler": (0, "registered\nverified: the job runner ran at 10:01:02")})
    r = fs.step_scheduler(ctx(tmp_path, runner=f))
    assert r["status"] == "done" and f.calls[0][-1] == "--apply" and r["detail"] == "verified: the job runner ran at 10:01:02"
    manual = fs.step_scheduler(ctx(tmp_path, runner=Fake({"install_scheduler": (3, "manual: no scheduler found")})))
    assert manual["status"] == "manual" and "no scheduler found" in manual["detail"]
    bad = fs.step_scheduler(ctx(tmp_path, runner=Fake({"install_scheduler": (1, "registered, but the job runner did not run yet")})))
    assert bad["status"] == "failed" and "job_runner.py --once" in bad["detail"] and "did not run yet" in bad["detail"]


# ---------- obsidian ----------
@pytest.mark.parametrize("where", [("Programs", "Obsidian"), ("Obsidian",)])
def test_windows_finds_obsidian_in_either_install_folder(tmp_path, where):
    c = ctx(tmp_path, env=with_obsidian(tmp_path, where))
    assert fs.obsidian_installed(c) and fs.step_obsidian(c)["status"] == "skipped"


def test_windows_finds_obsidian_under_program_files(tmp_path):
    pf = tmp_path / "pf"
    (pf / "Obsidian").mkdir(parents=True)
    (pf / "Obsidian" / "Obsidian.exe").write_text("")
    assert fs.obsidian_installed(ctx(tmp_path, env={"ProgramFiles": str(pf)}))


def test_obsidian_is_found_on_the_path_on_mac_and_linux_and_as_a_flatpak(tmp_path):
    assert fs.step_obsidian(ctx(tmp_path, platform="linux", which=lambda n: "/usr/bin/obsidian" if n == "obsidian" else None))["status"] == "skipped"
    home = tmp_path / "home"
    (home / "Applications" / "Obsidian.app").mkdir(parents=True)
    assert fs.step_obsidian(ctx(tmp_path, platform="darwin", home=home))["status"] == "skipped"
    flat = Fake({"flatpak list": (0, "Obsidian  md.obsidian.Obsidian  stable")})
    assert fs.step_obsidian(ctx(tmp_path, platform="linux", runner=flat, which=lambda n: n if n == "flatpak" else None))["status"] == "skipped"


def test_obsidian_is_installed_with_the_native_installer_of_each_system(tmp_path):
    w = Fake()
    assert fs.step_obsidian(ctx(tmp_path, runner=w))["status"] == "done"
    assert w.calls[-1][:5] == ["winget", "install", "-e", "--id", "Obsidian.Obsidian"] and "--silent" in w.calls[-1] and "--accept-package-agreements" in w.calls[-1]
    m = Fake()
    assert fs.step_obsidian(ctx(tmp_path, platform="darwin", runner=m, home=tmp_path / "nohome"))["status"] == "done"
    assert m.calls[-1] == ["brew", "install", "--cask", "obsidian"]
    l = Fake({"flatpak list": (0, "")})
    assert fs.step_obsidian(ctx(tmp_path, platform="linux", runner=l, which=lambda n: n if n == "flatpak" else None))["status"] == "done"
    assert l.calls[-1] == ["flatpak", "install", "--user", "-y", "flathub", "md.obsidian.Obsidian"]
    assert any(c[:4] == ["flatpak", "remote-add", "--user", "--if-not-exists"] for c in l.calls)


def test_a_winget_code_for_already_installed_is_not_a_failure(tmp_path):
    r = fs.step_obsidian(ctx(tmp_path, runner=Fake({"winget": (-1978335189, "No applicable upgrade found.\nNo newer package versions are available")})))
    assert r["status"] == "done" and "already there" in r["detail"]
    ok_text = fs.step_obsidian(ctx(tmp_path, runner=Fake({"winget": (43, "Found an existing package already installed.")})))
    assert ok_text["status"] == "done"


def test_without_any_installer_obsidian_is_a_manual_step_with_clear_words(tmp_path):
    r = fs.step_obsidian(ctx(tmp_path, which=lambda n: None))
    assert r["status"] == "manual" and "winget is missing" in r["detail"] and "obsidian.md" in r["detail"]
    r = fs.step_obsidian(ctx(tmp_path, platform="darwin", which=lambda n: None, home=tmp_path / "x"))
    assert r["status"] == "manual" and "Homebrew or Flatpak" in r["detail"]


def test_a_failed_obsidian_install_is_reported_as_failed(tmp_path):
    r = fs.step_obsidian(ctx(tmp_path, runner=Fake({"winget": (1, "source error")})))
    assert r["status"] == "failed" and "source error" in r["detail"]


# ---------- the vault in Obsidian ----------
def test_the_vault_is_registered_through_the_obsidian_module(tmp_path):
    f = Fake({"obsidian_vault": (0, "registered /x/vault")})
    c = ctx(tmp_path, runner=f, env=with_obsidian(tmp_path))
    r = fs.step_vault(c)
    assert r["status"] == "done" and f.calls[-1][1].endswith("obsidian_vault.py") and f.calls[-1][2:] == ["--vault", c.vault, "--register"]


def test_a_running_obsidian_makes_the_vault_step_manual_with_the_exact_next_command(tmp_path):
    r = fs.step_vault(ctx(tmp_path, runner=Fake({"obsidian_vault": (4, "Obsidian is running")}), env=with_obsidian(tmp_path)))
    assert r["status"] == "manual" and "close Obsidian" in r["detail"] and "obsidian_vault.py --vault vault --register" in r["detail"]


def test_a_failing_registration_is_a_failure(tmp_path):
    r = fs.step_vault(ctx(tmp_path, runner=Fake({"obsidian_vault": (1, "damaged obsidian.json")}), env=with_obsidian(tmp_path)))
    assert r["status"] == "failed" and "damaged obsidian.json" in r["detail"]


def test_without_obsidian_the_vault_step_is_manual_and_runs_nothing(tmp_path):
    f = Fake()
    r = fs.step_vault(ctx(tmp_path, runner=f))
    assert r["status"] == "manual" and f.calls == []


# ---------- trust ----------
def write_claude_json(home, data):
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(json.dumps(data), encoding="utf-8")


def key(c):
    return os.path.realpath(c.out).replace("\\", "/")


def test_the_new_folder_is_marked_trusted_other_entries_are_kept_and_a_backup_is_made(tmp_path):
    home = tmp_path / "home"
    write_claude_json(home, {"theme": "dark", "projects": {"C:/other": {"hasTrustDialogAccepted": False, "allowedTools": ["x"]}}})
    c = ctx(tmp_path, home=home)
    assert fs.step_trust(c)["status"] == "done"
    data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert data["projects"][key(c)]["hasTrustDialogAccepted"] is True
    assert data["theme"] == "dark" and data["projects"]["C:/other"] == {"hasTrustDialogAccepted": False, "allowedTools": ["x"]}
    assert json.loads((home / ".claude.json.bak_starter_kit").read_text(encoding="utf-8"))["projects"].keys() == {"C:/other"}
    assert fs.step_trust(c)["status"] == "skipped"


def test_the_first_backup_is_never_overwritten_by_a_later_run(tmp_path):
    home = tmp_path / "home"
    write_claude_json(home, {"projects": {}})
    fs.step_trust(ctx(tmp_path, home=home, name="one"))
    original = (home / ".claude.json.bak_starter_kit").read_text(encoding="utf-8")
    fs.step_trust(ctx(tmp_path, home=home, name="two"))
    assert (home / ".claude.json.bak_starter_kit").read_text(encoding="utf-8") == original
    assert len([p for p in home.iterdir() if p.name.startswith(".claude.json.bak_starter_kit")]) == 2


def test_an_existing_entry_for_the_folder_keeps_its_other_fields(tmp_path):
    home = tmp_path / "home"
    c = ctx(tmp_path, home=home)
    write_claude_json(home, {"projects": {key(c): {"allowedTools": ["Bash"], "hasTrustDialogAccepted": False}}})
    fs.step_trust(c)
    assert json.loads((home / ".claude.json").read_text(encoding="utf-8"))["projects"][key(c)] == {"allowedTools": ["Bash"], "hasTrustDialogAccepted": True}


@pytest.mark.skipif(os.name == "nt", reason="file modes are a POSIX idea")
def test_the_file_keeps_its_permissions(tmp_path):
    home = tmp_path / "home"
    write_claude_json(home, {"projects": {}})
    os.chmod(home / ".claude.json", 0o600)
    fs.step_trust(ctx(tmp_path, home=home))
    assert stat.S_IMODE(os.stat(home / ".claude.json").st_mode) == 0o600


def test_claude_config_dir_is_honoured(tmp_path):
    cfg = tmp_path / "cfg"
    write_claude_json(cfg, {"projects": {}})
    (cfg / ".claude.json").rename(cfg / ".claude.json")
    c = ctx(tmp_path, home=tmp_path / "home", env={"CLAUDE_CONFIG_DIR": str(cfg)})
    assert fs.claude_config_path(c) == str(cfg / ".claude.json") and fs.step_trust(c)["status"] == "done"
    assert not (tmp_path / "home" / ".claude.json").exists()


def test_both_spellings_of_the_folder_path_are_trusted_when_they_differ(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "realpath", lambda p: "/real/place/assistant")
    home = tmp_path / "home"
    write_claude_json(home, {"projects": {}})
    c = ctx(tmp_path, home=home)
    fs.step_trust(c)
    projects = json.loads((home / ".claude.json").read_text(encoding="utf-8"))["projects"]
    assert set(projects) == {"/real/place/assistant", os.path.abspath(c.out).replace("\\", "/")} and all(v["hasTrustDialogAccepted"] for v in projects.values())


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", json.dumps({"projects": []})])
def test_an_unreadable_settings_file_is_a_manual_step_and_is_never_overwritten(tmp_path, content):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(content, encoding="utf-8")
    r = fs.step_trust(ctx(tmp_path, home=home))
    assert r["status"] == "manual" and "answer yes" in r["detail"]
    assert (home / ".claude.json").read_text(encoding="utf-8") == content


def test_no_settings_file_at_all_is_a_manual_step(tmp_path):
    assert fs.step_trust(ctx(tmp_path, home=tmp_path / "empty"))["status"] == "manual"


def test_trust_only_marks_any_folder_and_always_exits_0(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    write_claude_json(home, {"projects": {}})
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home))
    kit = tmp_path / "kit"
    kit.mkdir()
    assert fs.main(["--trust-only", str(kit)]) == 0
    assert "DONE" in capsys.readouterr().out
    assert json.loads((home / ".claude.json").read_text(encoding="utf-8"))["projects"][os.path.realpath(kit).replace("\\", "/")]["hasTrustDialogAccepted"] is True
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "nobody"))
    assert fs.main(["--trust-only", str(kit)]) == 0


# ---------- parity ----------
def test_parity_passes_or_fails_by_the_exit_code(tmp_path):
    ok = fs.step_parity(ctx(tmp_path, runner=Fake({"parity_test": (0, "line\nRESULT: all 7 checks pass")})))
    assert ok["status"] == "done" and ok["detail"] == "RESULT: all 7 checks pass"
    bad = fs.step_parity(ctx(tmp_path, runner=Fake({"parity_test": (1, "4 safety     FAIL  canary failed")})))
    assert bad["status"] == "failed" and "canary failed" in bad["detail"]


# ---------- launch ----------
def test_launch_opens_the_vault_in_obsidian_and_a_terminal_in_the_folder_without_waiting_for_it(tmp_path):
    opened, launcher, f = [], Launcher(), Fake()
    c = ctx(tmp_path, runner=f, opener=opened.append, launcher=launcher, env=with_obsidian(tmp_path))
    r = fs.step_launch(c)
    assert r["status"] == "done" and len(opened) == 1 and opened[0].startswith("obsidian://open?path=") and "vault" in opened[0] and " " not in opened[0]
    assert len(launcher.calls) == 1 and f.calls == []                                   # the terminal goes through the launcher, never through the capturing runner
    cmd = launcher.calls[0]
    assert cmd == [fs.SHELL_EXECUTE, "cmd.exe", '/k claude "%s"' % fs.FIRST_PROMPT, c.out]


def test_a_windows_path_with_spaces_and_an_apostrophe_is_passed_as_the_working_folder_untouched(tmp_path):
    c = ctx(tmp_path, name="Ann's Assistant")
    assert fs.terminal_command(c)[3] == c.out and "Ann's Assistant" in c.out


def test_the_windows_launcher_goes_through_the_shell_and_reports_its_code(monkeypatch):
    import ctypes
    import types
    seen = []

    def fake(hwnd, verb, program, params, folder, show):
        seen.append((hwnd, verb, program, params, folder, show))
        return 42

    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(shell32=types.SimpleNamespace(ShellExecuteW=fake)), raising=False)
    assert fs.Context._launch([fs.SHELL_EXECUTE, "cmd.exe", '/k claude "hi"', "C:/x y"]) == (True, "started")
    assert seen == [(None, "open", "cmd.exe", '/k claude "hi"', "C:/x y", 1)]
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(shell32=types.SimpleNamespace(ShellExecuteW=lambda *a: 2)), raising=False)
    ok, detail = fs.Context._launch([fs.SHELL_EXECUTE, "nope.exe", "", "C:/x"])
    assert not ok and "could not open nope.exe (code 2)" in detail


@pytest.mark.skipif(os.name != "nt", reason="the Windows shell")
def test_a_console_program_opened_by_the_windows_launcher_gets_a_working_input_not_nul(tmp_path):
    """The bug this guards: a window opened with inherited NUL input closes at once. The program here waits for a line of input that never comes; if its input were NUL it would end
    in well under a second, and the marker file it writes at the end would appear."""
    import time
    marker = tmp_path / "ended.txt"
    script = tmp_path / "wait_for_input.py"
    script.write_text("import sys\nline = sys.stdin.readline()\nopen(sys.argv[1], 'w').write('input ended: %r' % line)\n", encoding="utf-8")
    ok, _detail = fs.Context._launch([fs.SHELL_EXECUTE, sys.executable, '"%s" "%s"' % (script, marker), str(tmp_path)])
    assert ok
    time.sleep(3)
    still_waiting = not marker.exists()
    if marker.exists():
        pytest.fail("the program's input ended at once: " + marker.read_text(encoding="utf-8"))
    assert still_waiting
    subprocess.run(["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*wait_for_input.py*' -and $_.Name -like 'python*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                   capture_output=True, creationflags=0x08000000)


def test_the_obsidian_address_has_spaces_encoded(tmp_path):
    opened = []
    c = ctx(tmp_path, name="my assistant", opener=opened.append, env=with_obsidian(tmp_path))
    fs.step_launch(c)
    assert "my%20assistant" in opened[0]


def test_launch_without_obsidian_is_a_manual_step_that_still_starts_the_assistant(tmp_path):
    opened, launcher = [], Launcher()
    r = fs.step_launch(ctx(tmp_path, opener=opened.append, launcher=launcher))
    assert opened == [] and len(launcher.calls) == 1
    assert r["status"] == "manual" and "Obsidian is not installed" in r["detail"] and "assistant started in a new terminal" in r["detail"]


def test_a_failing_opener_or_terminal_never_hides_the_finished_setup(tmp_path):
    def broken(target):
        raise OSError("no handler")

    r = fs.step_launch(ctx(tmp_path, opener=broken, launcher=Launcher(False, "powershell exited with code 1"), env=with_obsidian(tmp_path)))
    assert r["status"] == "manual" and "could not open Obsidian" in r["detail"] and "could not open a terminal here" in r["detail"] and "run: claude" in r["detail"]


def test_no_terminal_program_on_linux_is_a_manual_step(tmp_path):
    r = fs.step_launch(ctx(tmp_path, platform="linux", which=lambda n: None))
    assert r["status"] == "manual" and "no terminal program was found" in r["detail"]


def test_launch_re_applies_trust_when_claude_code_rewrote_its_settings(tmp_path):
    home = tmp_path / "home"
    write_claude_json(home, {"projects": {}})
    c = ctx(tmp_path, home=home, env=with_obsidian(tmp_path))
    r = fs.step_launch(c)
    assert "trust re-applied" in r["detail"] and json.loads((home / ".claude.json").read_text(encoding="utf-8"))["projects"][key(c)]["hasTrustDialogAccepted"] is True


def test_mac_terminal_command_quotes_a_path_with_spaces_and_an_apostrophe(tmp_path):
    c = ctx(tmp_path, platform="darwin", name="Ann Lee's Assistant")
    cmd = fs.terminal_command(c)
    assert cmd[0] == "osascript" and cmd[1] == "-e"
    script = cmd[2]
    assert script.startswith('tell application "Terminal"') and "do script" in script
    inner = script.split('do script "', 1)[1].rsplit('"\nend tell', 1)[0].replace('\\"', '"').replace("\\\\", "\\")
    assert shlex.split(inner)[:3] == ["cd", c.out, "&&"] and shlex.split(inner)[3] == "claude"


@pytest.mark.parametrize("term", ["gnome-terminal", "konsole", "xterm", "x-terminal-emulator"])
def test_linux_terminals_get_separate_arguments_and_a_quoted_path(tmp_path, term):
    c = ctx(tmp_path, platform="linux", which=lambda n: n if n == term else None, name="My Assistant")
    cmd = fs.terminal_command(c)
    assert cmd[0] == term and cmd[1:4] == (["--", "bash", "-c"] if term == "gnome-terminal" else ["-e", "bash", "-c"])
    assert len(cmd) == 5 and shlex.split(cmd[4].split("; exec")[0])[:2] == ["cd", c.out]


def test_no_terminal_means_none(tmp_path):
    assert fs.terminal_command(ctx(tmp_path, platform="linux", which=lambda n: None)) is None


# ---------- the real launcher ----------
def test_the_real_launcher_returns_at_once_while_the_program_keeps_running(tmp_path):
    import time
    t0 = time.time()
    ok, detail = fs.Context._launch([sys.executable, "-c", "import time; time.sleep(12)"])
    assert ok and detail == "started" and 7 < time.time() - t0 < 11              # waited a few seconds to see it did not fail at once, then left it running


def test_the_real_launcher_reports_a_program_that_fails_at_once_and_one_that_is_missing():
    ok, detail = fs.Context._launch([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert not ok and "exited with code 3" in detail
    ok, detail = fs.Context._launch(["definitely-not-a-program-xyz"])
    assert not ok and "could not start" in detail


# ---------- the runner of steps ----------
def test_plan_respects_skip_and_only_in_the_fixed_order():
    assert fs.plan()[0] == "pytest" and fs.plan()[-1] == "launch" and len(fs.plan()) == 8
    assert fs.plan().index("vault") > fs.plan().index("obsidian") and fs.plan().index("trust") > fs.plan().index("parity")
    assert "obsidian" not in fs.plan(skip=["obsidian"]) and fs.plan(only=["parity", "pytest"]) == ["pytest", "parity"]


def test_a_step_that_raises_is_a_failed_step_and_the_rest_still_run(tmp_path, monkeypatch):
    def boom(c):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(fs, "STEPS", [("pytest", boom), ("headless", lambda c: fs.result("headless", "done", "ok"))])
    results = fs.run_steps(ctx(tmp_path), ["pytest", "headless"])
    assert [r["status"] for r in results] == ["failed", "done"] and "kaboom" in results[0]["detail"]


def test_a_step_that_finished_before_is_not_repeated_unless_forced_but_parity_and_launch_always_run(tmp_path, monkeypatch):
    ran = []
    steps = [(n, (lambda n: lambda c: ran.append(n) or fs.result(n, "done", "x"))(n)) for n in ("pytest", "scheduler", "parity", "launch")]
    monkeypatch.setattr(fs, "STEPS", steps)
    state = {}
    fs.run_steps(ctx(tmp_path), ["pytest", "scheduler", "parity", "launch"], state)
    assert ran == ["pytest", "scheduler", "parity", "launch"] and state["pytest"]["status"] == "done"
    ran.clear()
    second = fs.run_steps(ctx(tmp_path), ["pytest", "scheduler", "parity", "launch"], state)
    assert ran == ["parity", "launch"] and [r["status"] for r in second] == ["skipped", "skipped", "done", "done"] and "earlier run" in second[0]["detail"]
    assert state["pytest"]["detail"] == "x"                                              # the record keeps what the first run did
    ran.clear()
    fs.run_steps(ctx(tmp_path), ["pytest", "scheduler"], state, force=True)
    assert ran == ["pytest", "scheduler"]


def test_a_failed_or_manual_step_is_tried_again_on_the_next_run(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(fs, "STEPS", [("obsidian", lambda c: calls.append(1) or fs.result("obsidian", "manual", "x"))])
    state = {}
    fs.run_steps(ctx(tmp_path), ["obsidian"], state)
    fs.run_steps(ctx(tmp_path), ["obsidian"], state)
    assert len(calls) == 2


def test_each_result_is_reported_as_soon_as_its_step_ends(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(fs, "STEPS", [("pytest", lambda c: fs.result("pytest", "done", "a")), ("headless", lambda c: fs.result("headless", "done", "b"))])
    fs.run_steps(ctx(tmp_path), ["pytest", "headless"], report=lambda r: seen.append(r["name"]))
    assert seen == ["pytest", "headless"]


def test_the_report_shows_every_line_of_a_detail_not_only_the_first():
    text = fs.format_result(fs.result("pytest", "failed", "pip install pytest failed:\nline two\nline three"))
    lines = text.splitlines()
    assert lines[0].startswith("pytest     FAILED   pip install pytest failed:") and lines[1].strip() == "line two" and lines[2].strip() == "line three"


def test_the_summary_line_says_whether_everything_is_set_up():
    assert fs.format_results([fs.result("a", "done", "x"), fs.result("b", "skipped", "y")]).splitlines()[-1] == "RESULT: everything is set up"
    mixed = fs.format_results([fs.result("a", "failed", "x"), fs.result("b", "manual", "y")])
    assert "failed: a" in mixed.splitlines()[-1] and "manual step: b" in mixed.splitlines()[-1]


def test_main_prints_the_plan_without_running_anything_unless_yes(tmp_path, capsys, monkeypatch):
    called = []
    monkeypatch.setattr(fs, "run_steps", lambda c, names, state=None, force=False, report=None: called.append(names) or [])
    (tmp_path / "scripts").mkdir()
    assert fs.main(["--out", str(tmp_path)]) == 0 and called == []
    out = capsys.readouterr().out
    assert "plan (nothing is run without --yes)" in out and "headless_subprocess.py --uninstall" in out
    assert fs.main(["--out", str(tmp_path), "--yes", "--only", "parity"]) == 0 and called == [["parity"]]


def test_main_writes_the_report_and_the_state_and_status_shows_them(tmp_path, capsys, monkeypatch):
    (tmp_path / "scripts").mkdir()
    monkeypatch.setattr(fs, "STEPS", [("pytest", lambda c: fs.result("pytest", "done", "installed"))])
    assert fs.main(["--out", str(tmp_path), "--yes"]) == 0
    assert "RESULT: everything is set up" in (tmp_path / "logs" / "finish_setup.txt").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "logs" / "finish_setup.json").read_text(encoding="utf-8"))["pytest"]["status"] == "done"
    capsys.readouterr()
    assert fs.main(["--out", str(tmp_path), "--status"]) == 0
    shown = capsys.readouterr().out
    assert shown.startswith("pytest     DONE ") and shown.rstrip().endswith("installed")


def test_main_exit_codes_for_failure_bad_step_names_and_a_folder_that_is_not_built(tmp_path, monkeypatch, capsys):
    (tmp_path / "scripts").mkdir()
    monkeypatch.setattr(fs, "run_steps", lambda c, names, state=None, force=False, report=None: [fs.result("parity", "failed", "x")])
    assert fs.main(["--out", str(tmp_path), "--yes"]) == 1
    assert fs.main(["--out", str(tmp_path), "--only", "nonsense"]) == 2
    assert fs.main(["--out", str(tmp_path / "missing")]) == 2
    assert fs.main([]) == 2


def test_the_real_runner_runs_a_plain_command_and_a_shell_string_and_decodes_utf8(tmp_path):
    code, out = fs.Context._run([sys.executable, "-c", "print('hi')"])
    assert code == 0 and out == "hi"
    code, out = fs.Context._run("echo shell-string")
    assert code == 0 and "shell-string" in out
    code, out = fs.Context._run([sys.executable, "-c", "print('\u041f\u0440\u0438\u0432\u0435\u0442 caf\u00e9')"])
    assert code == 0 and out == "\u041f\u0440\u0438\u0432\u0435\u0442 caf\u00e9"


def test_the_real_runner_gives_a_child_no_stdin(tmp_path):
    code, out = fs.Context._run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"])
    assert code == 0 and out == "''"
