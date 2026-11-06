# -*- coding: utf-8 -*-
"""Tests for startup_programs_audit.py. The registry is an in-memory fake and the Startup folders are folders in a temp directory; nothing real is read."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import startup_programs_audit as spa  # noqa: E402

RUN = spa.RUN
RUN_ONCE = spa.RUN_ONCE
POLICY = spa.POLICY_RUN


class _Key:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """An in-memory registry. tree = {(hive, subkey, view): [(name, value, type), ...]} with view 0, 'x64' or 'x32'."""
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 1
    KEY_WOW64_64KEY = 256
    KEY_WOW64_32KEY = 512

    def __init__(self, tree):
        self.tree = tree
        self.opened = []

    def OpenKey(self, hive, subkey, reserved=0, access=1):
        view = "x64" if access & 256 else "x32" if access & 512 else 0
        self.opened.append((hive, subkey, view))
        try:
            return _Key(self.tree[(hive, subkey, view)])
        except KeyError:
            raise FileNotFoundError(subkey)

    def EnumValue(self, key, index):
        if index >= len(key.values):
            raise OSError("no more data")
        return key.values[index]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    appdata = tmp_path / "AppData"
    programdata = tmp_path / "ProgramData"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("PROGRAMDATA", str(programdata))
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(spa, "winreg", FakeWinreg({}))
    return tmp_path


def reg(monkeypatch, tree):
    fake = FakeWinreg(tree)
    monkeypatch.setattr(spa, "winreg", fake)
    return fake


# ---------- the pointer ----------
@pytest.mark.parametrize("command", [
    "C:\\Windows\\Temp\\x.exe", "%TEMP%\\update.exe", "c:/users/x/downloads/setup.exe", "powershell -nop -w hidden -c calc", "wscript //b x.vbs",
    "mshta http://example.test/a.hta", "cmd.exe /c start x", "powershell -enc AAAA", "rundll32 https://example.test/x",
])
def test_worth_a_look_marks_temp_folders_script_hosts_and_web_addresses(command):
    assert spa.worth_a_look(command) != ""


@pytest.mark.parametrize("command", ["\"C:\\Program Files\\OneDrive\\OneDrive.exe\" /background", "C:\\Apps\\tool.exe --tray", "", None])
def test_worth_a_look_leaves_ordinary_programs_alone(command):
    assert spa.worth_a_look(command) == ""


# ---------- registry ----------
def test_run_keys_of_both_hives_are_listed_with_their_source_labels(monkeypatch):
    fake = reg(monkeypatch, {
        ("HKCU", RUN, 0): [("OneDrive", "od.exe /background", 1), ("Chat App", "chat.exe", 1)],
        ("HKLM", RUN, "x64"): [("SecurityHealth", "sh.exe", 1)],
    })
    items = spa.list_startup_items()
    assert [(i["source"], i["name"], i["command"]) for i in items] == [
        ("HKCU Run", "OneDrive", "od.exe /background"), ("HKCU Run", "Chat App", "chat.exe"), ("HKLM Run", "SecurityHealth", "sh.exe")]
    assert ("HKLM", RUN, "x64") in fake.opened


def test_the_32_bit_view_runonce_and_policy_keys_are_read_too(monkeypatch):
    reg(monkeypatch, {
        ("HKLM", RUN, "x32"): [("Old32", "old32.exe", 1)],
        ("HKCU", RUN_ONCE, 0): [("Once", "once.exe", 1)],
        ("HKLM", POLICY, "x64"): [("Policy", "policy.exe", 1)],
        ("HKCU", POLICY, 0): [("UserPolicy", "up.exe", 1)],
    })
    assert {(i["source"], i["name"]) for i in spa.list_startup_items()} == {
        ("HKLM Run (32-bit)", "Old32"), ("HKCU RunOnce", "Once"), ("HKLM Policies Run", "Policy"), ("HKCU Policies Run", "UserPolicy")}


def test_the_same_name_in_two_keys_stays_two_entries(monkeypatch):
    reg(monkeypatch, {("HKCU", RUN, 0): [("Updater", "user.exe", 1)], ("HKLM", RUN, "x64"): [("Updater", "machine.exe", 1)]})
    assert [(i["source"], i["command"]) for i in spa.list_startup_items()] == [("HKCU Run", "user.exe"), ("HKLM Run", "machine.exe")]


def test_a_missing_or_unreadable_key_is_skipped_not_fatal(monkeypatch):
    fake = reg(monkeypatch, {("HKCU", RUN, 0): [("A", "a.exe", 1)]})
    real_open = fake.OpenKey

    def open_key(hive, subkey, reserved=0, access=1):
        if hive == "HKLM":
            raise PermissionError("denied")
        return real_open(hive, subkey, reserved, access)
    fake.OpenKey = open_key
    assert [i["name"] for i in spa.list_startup_items()] == ["A"]


def test_non_text_values_and_empty_names_are_made_printable(monkeypatch):
    reg(monkeypatch, {("HKCU", RUN, 0): [("", b"\x01\x02", 3), ("Dword", 1, 4)]})
    items = spa.list_startup_items()
    assert items[0]["name"] == "(default)" and isinstance(items[0]["command"], str) and items[1]["command"] == "1"


def test_a_huge_value_is_cut(monkeypatch):
    reg(monkeypatch, {("HKCU", RUN, 0): [("Big", "x" * 5000, 1)]})
    assert len(spa.list_startup_items()[0]["command"]) == spa.MAX_COMMAND


# ---------- folders ----------
def test_both_startup_folders_are_listed_and_desktop_ini_is_skipped(env):
    user = env / "AppData" / spa.STARTUP_SUB
    common = env / "ProgramData" / spa.STARTUP_SUB
    user.mkdir(parents=True)
    common.mkdir(parents=True)
    (user / "b.lnk").write_bytes(b"x")
    (user / "a.lnk").write_bytes(b"x")
    (user / "desktop.ini").write_text("[x]", encoding="utf-8")
    (common / "shared.lnk").write_bytes(b"x")
    items = spa.list_startup_items()
    assert [(i["source"], i["name"]) for i in items] == [("Startup folder (this user)", "a.lnk"), ("Startup folder (this user)", "b.lnk"), ("Startup folder (all users)", "shared.lnk")]
    assert items[0]["command"] == str(user / "a.lnk")


def test_a_missing_startup_folder_is_fine(env):
    assert spa.list_startup_items() == []


def test_without_appdata_the_home_folder_is_used(monkeypatch, tmp_path):
    monkeypatch.delenv("APPDATA")
    monkeypatch.delenv("PROGRAMDATA")
    monkeypatch.setattr(spa.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert spa.folders() == [("Startup folder (this user)", tmp_path / "home" / "AppData" / "Roaming" / spa.STARTUP_SUB)]


def test_the_listing_never_reads_the_content_of_a_startup_file(env, monkeypatch):
    user = env / "AppData" / spa.STARTUP_SUB
    user.mkdir(parents=True)
    (user / "x.lnk").write_bytes(b"secret=1")
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a file was opened")))
    assert spa.list_startup_items()[0]["name"] == "x.lnk"


# ---------- printing and the command line ----------
def test_format_item_flags_and_neutralises_control_characters():
    line = spa.format_item({"source": "HKCU Run", "name": "n\x1b[2J", "command": "powershell -c x\r\nfake line", "look": "starts through a script host or a web address"})
    assert "\n" not in line and "\r" not in line and "\x1b" not in line and "worth a look" in line


def test_main_prints_a_count_and_every_entry(monkeypatch, capsys):
    reg(monkeypatch, {("HKCU", RUN, 0): [("OneDrive", "od.exe", 1), ("Odd", "powershell -c x", 1)]})
    assert spa.main([]) == 0
    out = capsys.readouterr().out
    assert "2 startup item(s), 1 worth a look" in out and "[HKCU Run] OneDrive: od.exe" in out and "worth a look" in out


def test_main_json_is_valid_json_of_the_same_list(monkeypatch, capsys):
    reg(monkeypatch, {("HKCU", RUN, 0): [("OneDrive", "od.exe", 1)]})
    assert spa.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == [{"source": "HKCU Run", "name": "OneDrive", "command": "od.exe", "look": ""}]


def test_off_windows_it_says_so_and_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert spa.main([]) == 0
    out = capsys.readouterr().out
    assert "Windows only" in out and "persistence_audit" in out and len(out.strip().splitlines()) == 1


def test_without_the_winreg_module_it_says_so_and_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(spa, "winreg", None)
    assert spa.main([]) == 0
    assert "Windows only" in capsys.readouterr().out
    assert spa.list_startup_items() == []                        # the folder half still works without the module


def test_main_returns_1_only_when_the_script_itself_breaks(monkeypatch, capsys):
    monkeypatch.setattr(spa, "list_startup_items", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert spa.main([]) == 1 and "failed" in capsys.readouterr().out


def test_the_script_imports_and_exits_0_as_a_program_whatever_the_system(tmp_path):
    envv = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="utf-8",
                APPDATA=str(tmp_path / "AppData"), PROGRAMDATA=str(tmp_path / "ProgramData"), HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    r = subprocess.run([sys.executable, "-B", os.path.join(os.path.dirname(os.path.abspath(__file__)), "startup_programs_audit.py"), "--help"], env=envv, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "startup" in r.stdout.lower()
