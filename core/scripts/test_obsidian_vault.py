#!/usr/bin/env python3
"""
Tests for obsidian_vault.py

Run with: python -m pytest test_obsidian_vault.py -q
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add the current directory to path
sys.path.insert(0, os.path.dirname(__file__))

import obsidian_vault as ov


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def tmp_home(tmp_path):
    """Create a temporary home directory."""
    home = tmp_path / "home"
    home.mkdir()
    return str(home)


@pytest.fixture
def windows_env():
    """Windows-like environment."""
    return {
        "LOCALAPPDATA": r"C:\Users\test\AppData\Local",
        "APPDATA": r"C:\Users\test\AppData\Roaming",
        "ProgramFiles": r"C:\Program Files",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
    }


@pytest.fixture
def macos_env(tmp_home):
    """macOS-like environment."""
    return {"HOME": tmp_home}


@pytest.fixture
def linux_env(tmp_home):
    """Linux-like environment."""
    return {"HOME": tmp_home, "XDG_CONFIG_HOME": ""}


# ============================================================
# find_obsidian tests
# ============================================================

def test_find_obsidian_windows_localappdata(tmp_home, windows_env):
    """Find Obsidian in LOCALAPPDATA on Windows."""
    # Point LOCALAPPDATA to tmp_home
    env = dict(windows_env)
    env["LOCALAPPDATA"] = str(Path(tmp_home) / "Local")
    obsidian_exe = Path(tmp_home) / "Local" / "Programs" / "Obsidian" / "Obsidian.exe"
    obsidian_exe.parent.mkdir(parents=True)
    obsidian_exe.touch()

    def exists(path):
        return Path(path).exists()

    result = ov.find_obsidian("windows", env, tmp_home, exists, lambda cmd: (0, ""))
    assert result == str(obsidian_exe)


def test_find_obsidian_windows_program_files(tmp_home, windows_env):
    """Find Obsidian in Program Files on Windows."""
    # Remove LOCALAPPDATA, point ProgramFiles to tmp_home
    env = {k: v for k, v in windows_env.items() if k != "LOCALAPPDATA"}
    env["ProgramFiles"] = str(Path(tmp_home) / "Program Files")
    obsidian_exe = Path(tmp_home) / "Program Files" / "Obsidian" / "Obsidian.exe"
    obsidian_exe.parent.mkdir(parents=True)
    obsidian_exe.touch()

    def exists(path):
        return Path(path).exists()

    result = ov.find_obsidian("windows", env, tmp_home, exists, lambda cmd: (0, ""))
    assert result == str(obsidian_exe)


def test_find_obsidian_windows_program_files_x86(tmp_home, windows_env):
    """Find Obsidian in Program Files (x86) on Windows."""
    env = {k: v for k, v in windows_env.items() if k not in ("LOCALAPPDATA", "ProgramFiles")}
    env["ProgramFiles(x86)"] = str(Path(tmp_home) / "Program Files (x86)")
    obsidian_exe = Path(tmp_home) / "Program Files (x86)" / "Obsidian" / "Obsidian.exe"
    obsidian_exe.parent.mkdir(parents=True)
    obsidian_exe.touch()

    def exists(path):
        return Path(path).exists()

    result = ov.find_obsidian("windows", env, tmp_home, exists, lambda cmd: (0, ""))
    assert result == str(obsidian_exe)


def test_find_obsidian_windows_not_found(windows_env):
    """Return None when Obsidian not found on Windows."""
    def exists(path):
        return False

    result = ov.find_obsidian("windows", windows_env, "C:\\Users\\test", exists, lambda cmd: (0, ""))
    assert result is None


def test_find_obsidian_macos_applications(tmp_home, macos_env):
    """Find Obsidian in /Applications on macOS."""
    obsidian_app = Path("/Applications/Obsidian.app")
    # Can't create /Applications in test, so mock exists
    def exists(path):
        return path == "/Applications/Obsidian.app"

    result = ov.find_obsidian("darwin", macos_env, tmp_home, exists, lambda cmd: (0, ""))
    assert result == "/Applications/Obsidian.app"


def test_find_obsidian_macos_home_applications(tmp_home, macos_env):
    """Find Obsidian in ~/Applications on macOS."""
    obsidian_app = Path(tmp_home) / "Applications" / "Obsidian.app"
    obsidian_app.parent.mkdir(parents=True)
    obsidian_app.mkdir()  # It's a directory on macOS

    def exists(path):
        return Path(path).exists()

    result = ov.find_obsidian("darwin", macos_env, tmp_home, exists, lambda cmd: (0, ""))
    assert result == str(obsidian_app)


def test_find_obsidian_macos_not_found(macos_env):
    """Return None when Obsidian not found on macOS."""
    def exists(path):
        return False

    result = ov.find_obsidian("darwin", macos_env, "/Users/test", exists, lambda cmd: (0, ""))
    assert result is None


def test_find_obsidian_linux_which(tmp_home, linux_env):
    """Find Obsidian via which on Linux."""
    def which(cmd):
        if cmd == "obsidian":
            return "/usr/bin/obsidian"
        return None

    def exists(path):
        return path == "/usr/bin/obsidian"

    result = ov.find_obsidian("linux", linux_env, tmp_home, exists, lambda cmd: (0, ""), which)
    assert result == "/usr/bin/obsidian"


def test_find_obsidian_linux_flatpak(tmp_home, linux_env):
    """Find Obsidian via flatpak on Linux."""
    def which(cmd):
        return None

    def runner(cmd):
        if cmd == ["flatpak", "list", "--app"]:
            return (0, "md.obsidian.Obsidian\tstable\tflathub")
        return (1, "")

    def exists(path):
        return False

    result = ov.find_obsidian("linux", linux_env, tmp_home, exists, runner, which)
    assert result == "flatpak:md.obsidian.Obsidian"


def test_find_obsidian_linux_appimage_applications(tmp_home, linux_env):
    """Find Obsidian AppImage in ~/Applications on Linux."""
    appimage = Path(tmp_home) / "Applications" / "Obsidian-1.5.0.AppImage"
    appimage.parent.mkdir(parents=True)
    appimage.touch()

    def which(cmd):
        return None

    def runner(cmd):
        return (1, "")

    def exists(path):
        return Path(path).exists()

    result = ov.find_obsidian("linux", linux_env, tmp_home, exists, runner, which)
    assert result == str(appimage)


def test_find_obsidian_linux_appimage_downloads(tmp_home, linux_env):
    """Find Obsidian AppImage in ~/Downloads on Linux."""
    appimage = Path(tmp_home) / "Downloads" / "Obsidian-latest.AppImage"
    appimage.parent.mkdir(parents=True)
    appimage.touch()

    def which(cmd):
        return None

    def runner(cmd):
        return (1, "")

    def exists(path):
        return Path(path).exists()

    result = ov.find_obsidian("linux", linux_env, tmp_home, exists, runner, which)
    assert result == str(appimage)


def test_find_obsidian_linux_snap(tmp_home, linux_env):
    """Find Obsidian via snap on Linux."""
    def which(cmd):
        return None

    def runner(cmd):
        if cmd == ["flatpak", "list", "--app"]:
            return (1, "")
        if cmd == ["snap", "list", "obsidian"]:
            return (0, "Name  Version  Rev  Tracking  Publisher  Notes\nobsidian  1.5.0  123  stable  obsidian  -")
        return (1, "")

    def exists(path):
        return False

    result = ov.find_obsidian("linux", linux_env, tmp_home, exists, runner, which)
    assert result == "snap:obsidian"


def test_find_obsidian_linux_not_found(tmp_home, linux_env):
    """Return None when Obsidian not found on Linux."""
    def which(cmd):
        return None

    def runner(cmd):
        return (1, "")

    def exists(path):
        return False

    result = ov.find_obsidian("linux", linux_env, tmp_home, exists, runner, which)
    assert result is None


# ============================================================
# config_path tests
# ============================================================

def test_config_path_windows(windows_env, tmp_home):
    """Get config path on Windows."""
    def exists(path):
        return False

    result = ov.config_path("windows", windows_env, tmp_home, exists)
    expected = os.path.join(windows_env["APPDATA"], "obsidian", "obsidian.json")
    assert result == expected


def test_config_path_windows_no_appdata(tmp_home):
    """Fallback config path on Windows when APPDATA not set."""
    env = {"ProgramFiles": r"C:\Program Files"}
    def exists(path):
        return False

    result = ov.config_path("windows", env, tmp_home, exists)
    expected = os.path.join(tmp_home, "AppData", "Roaming", "obsidian", "obsidian.json")
    assert result == expected


def test_config_path_macos(macos_env, tmp_home):
    """Get config path on macOS."""
    def exists(path):
        return False

    result = ov.config_path("darwin", macos_env, tmp_home, exists)
    expected = os.path.join(tmp_home, "Library", "Application Support", "obsidian", "obsidian.json")
    assert result == expected


def test_config_path_linux_xdg(linux_env, tmp_home):
    """Get config path on Linux with XDG_CONFIG_HOME."""
    env = {"HOME": tmp_home, "XDG_CONFIG_HOME": os.path.join(tmp_home, ".config")}
    def exists(path):
        return False

    result = ov.config_path("linux", env, tmp_home, exists)
    expected = os.path.join(tmp_home, ".config", "obsidian", "obsidian.json")
    assert result == expected


def test_config_path_linux_default(tmp_home):
    """Get default config path on Linux."""
    env = {"HOME": tmp_home}
    def exists(path):
        return False

    result = ov.config_path("linux", env, tmp_home, exists)
    expected = os.path.join(tmp_home, ".config", "obsidian", "obsidian.json")
    assert result == expected


def test_config_path_linux_flatpak_exists(tmp_home):
    """Get flatpak config path when it exists on Linux."""
    flatpak_config = Path(tmp_home) / ".var" / "app" / "md.obsidian.Obsidian" / "config" / "obsidian" / "obsidian.json"
    flatpak_config.parent.mkdir(parents=True)
    flatpak_config.touch()

    env = {"HOME": tmp_home}
    def exists(path):
        return Path(path).exists()

    result = ov.config_path("linux", env, tmp_home, exists)
    assert result == str(flatpak_config)


# ============================================================
# is_running tests
# ============================================================

def test_is_running_windows_true():
    """Detect running Obsidian on Windows."""
    def runner(cmd):
        if cmd == ["tasklist", "/FI", "IMAGENAME eq Obsidian.exe"]:
            return (0, "Image Name                     PID Session Name        Session#    Mem Usage\n========================= ======== ================ =========== ============\nObsidian.exe                  1234 Console                    1      50,000 K")
        return (1, "")

    result = ov.is_running("windows", runner)
    assert result is True


def test_is_running_windows_false():
    """Detect Obsidian not running on Windows."""
    def runner(cmd):
        return (0, "No tasks found.")

    result = ov.is_running("windows", runner)
    assert result is False


def test_is_running_linux_pgrep_exact():
    """Detect running Obsidian on Linux with pgrep -x."""
    def runner(cmd):
        if cmd == ["pgrep", "-x", "Obsidian"]:
            return (0, "1234\n")
        if cmd == ["pgrep", "-xi", "obsidian"]:
            return (0, "")
        return (1, "")

    result = ov.is_running("linux", runner)
    assert result is True


def test_is_running_linux_pgrep_case_insensitive():
    """Detect running Obsidian on Linux with pgrep -xi."""
    def runner(cmd):
        # The function tries -x first, then -xi
        if cmd == ["pgrep", "-x", "Obsidian"]:
            return (1, "")
        if cmd == ["pgrep", "-xi", "obsidian"]:
            return (0, "1234\n")
        return (1, "")

    result = ov.is_running("linux", runner)
    assert result is True


def test_is_running_linux_not_running():
    """Detect Obsidian not running on Linux."""
    def runner(cmd):
        return (1, "")

    result = ov.is_running("linux", runner)
    assert result is False


def test_is_running_darwin_pgrep_case_insensitive():
    """Detect running Obsidian on macOS with pgrep -xi."""
    def runner(cmd):
        # The function tries -x first, then -xi
        if cmd == ["pgrep", "-x", "Obsidian"]:
            return (1, "")
        if cmd == ["pgrep", "-xi", "obsidian"]:
            return (0, "1234\n")
        return (1, "")

    result = ov.is_running("darwin", runner)
    assert result is True


# ============================================================
# register_vault tests
# ============================================================

def test_register_vault_new(tmp_path):
    """Register a new vault."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")
    assert result == "registered"

    with open(config_file, "r") as f:
        data = json.load(f)
    assert "vaults" in data
    assert "abcdef1234567890" in data["vaults"]
    assert data["vaults"]["abcdef1234567890"]["path"] == vault_path
    assert data["vaults"]["abcdef1234567890"]["ts"] == 1234567890000
    assert data["vaults"]["abcdef1234567890"]["open"] is True


def test_register_vault_already_registered(tmp_path):
    """Register a vault that's already registered."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    # Pre-populate config
    initial_data = {
        "vaults": {
            "existing1234567890": {
                "path": vault_path,
                "ts": 1000000000000,
                "open": False,
            }
        },
        "customKey": "customValue",  # Unknown key to preserve
    }
    with open(config_file, "w") as f:
        json.dump(initial_data, f)

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")
    assert result == "already registered"

    with open(config_file, "r") as f:
        data = json.load(f)
    assert data["vaults"]["existing1234567890"]["open"] is True
    assert data["vaults"]["existing1234567890"]["ts"] == 1000000000000  # Unchanged
    assert data["customKey"] == "customValue"  # Preserved


def test_register_vault_case_insensitive_windows(tmp_path):
    """Register vault already listed with different case on Windows."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "MyVault")
    Path(vault_path).mkdir()

    # Pre-populate with different case
    initial_data = {
        "vaults": {
            "existing1234567890": {
                "path": str(tmp_path / "myvault"),  # lowercase
                "ts": 1000000000000,
                "open": False,
            }
        },
    }
    with open(config_file, "w") as f:
        json.dump(initial_data, f)

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "windows")
    assert result == "already registered"

    with open(config_file, "r") as f:
        data = json.load(f)
    assert data["vaults"]["existing1234567890"]["open"] is True


def test_register_vault_different_slashes(tmp_path):
    """Register vault already listed with different slashes."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    # Pre-populate with backslashes
    initial_data = {
        "vaults": {
            "existing1234567890": {
                "path": str(tmp_path / "myvault").replace("/", "\\"),
                "ts": 1000000000000,
                "open": False,
            }
        },
    }
    with open(config_file, "w") as f:
        json.dump(initial_data, f)

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")
    assert result == "already registered"


def test_register_vault_trailing_slash(tmp_path):
    """Register vault already listed with trailing slash."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    # Pre-populate with trailing slash
    initial_data = {
        "vaults": {
            "existing1234567890": {
                "path": vault_path + "/",
                "ts": 1000000000000,
                "open": False,
            }
        },
    }
    with open(config_file, "w") as f:
        json.dump(initial_data, f)

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")
    assert result == "already registered"


def test_register_vault_missing_file(tmp_path):
    """Register when config file doesn't exist."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")
    assert result == "registered"

    with open(config_file, "r") as f:
        data = json.load(f)
    assert "vaults" in data
    assert "abcdef1234567890" in data["vaults"]


def test_register_vault_malformed_json(tmp_path):
    """Raise ValueError on malformed JSON."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    # Write invalid JSON
    with open(config_file, "w") as f:
        f.write("{ invalid json }")

    with pytest.raises(ValueError, match="Invalid JSON"):
        ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")

    # File should be untouched
    with open(config_file, "r") as f:
        content = f.read()
    assert content == "{ invalid json }"


def test_register_vault_not_object(tmp_path):
    """Raise ValueError when top level is not an object."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    # Write array instead of object
    with open(config_file, "w") as f:
        f.write("[]")

    with pytest.raises(ValueError, match="not an object"):
        ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")


def test_register_vault_preserves_unknown_keys(tmp_path):
    """Preserve unknown keys in config file."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    initial_data = {
        "vaults": {},
        "theme": "dark",
        "customPlugin": {"enabled": True},
        "someArray": [1, 2, 3],
    }
    with open(config_file, "w") as f:
        json.dump(initial_data, f)

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")
    assert result == "registered"

    with open(config_file, "r") as f:
        data = json.load(f)
    assert data["theme"] == "dark"
    assert data["customPlugin"] == {"enabled": True}
    assert data["someArray"] == [1, 2, 3]


def test_register_vault_creates_parent_dir(tmp_path):
    """Create parent directory if missing."""
    config_file = str(tmp_path / "config" / "obsidian" / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")
    assert result == "registered"

    assert Path(config_file).exists()


def test_register_vault_backup_created(tmp_path):
    """Create backup file before writing."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    # Pre-populate
    with open(config_file, "w") as f:
        json.dump({"vaults": {}}, f)

    # Mock time to get predictable backup name
    with patch("time.strftime", return_value="20240101-120000"):
        result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")

    assert result == "registered"

    backup_file = str(tmp_path / "obsidian.json.bak_starter_kit.20240101-120000")
    assert Path(backup_file).exists()

    # Verify backup content
    with open(backup_file, "r") as f:
        backup_data = json.load(f)
    assert backup_data == {"vaults": {}}


def test_register_vault_backup_not_overwrite_older(tmp_path):
    """Never overwrite an older backup."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    # Pre-populate
    with open(config_file, "w") as f:
        json.dump({"vaults": {}}, f)

    # Create older backup manually
    old_backup = str(tmp_path / "obsidian.json.bak_starter_kit.20240101-100000")
    with open(old_backup, "w") as f:
        f.write("old backup")

    with patch("time.strftime", return_value="20240101-120000"):
        result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")

    assert result == "registered"

    # Old backup should be unchanged
    with open(old_backup, "r") as f:
        content = f.read()
    assert content == "old backup"

    # New backup should exist
    new_backup = str(tmp_path / "obsidian.json.bak_starter_kit.20240101-120000")
    assert Path(new_backup).exists()


def test_register_vault_windows_path_separators(tmp_path):
    """Store path with Windows separators on Windows."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "windows")
    assert result == "registered"

    with open(config_file, "r") as f:
        data = json.load(f)
    stored_path = data["vaults"]["abcdef1234567890"]["path"]
    assert "\\" in stored_path
    assert "/" not in stored_path or stored_path == vault_path.replace("/", "\\")


def test_register_vault_unicode_path(tmp_path):
    """Handle unicode in vault path."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "我的笔记" / "vault")
    Path(vault_path).mkdir(parents=True)

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")
    assert result == "registered"

    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["vaults"]["abcdef1234567890"]["path"] == vault_path


def test_register_vault_space_in_path(tmp_path):
    """Handle spaces in vault path."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "My Vault")
    Path(vault_path).mkdir()

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")
    assert result == "registered"

    with open(config_file, "r") as f:
        data = json.load(f)
    assert data["vaults"]["abcdef1234567890"]["path"] == vault_path


# ============================================================
# open_uri tests
# ============================================================

def test_open_uri_basic():
    """Generate basic obsidian:// URI."""
    result = ov.open_uri("/home/user/vault")
    assert result == "obsidian://open?path=/home/user/vault"


def test_open_uri_windows_path():
    """Convert Windows path to forward slashes in URI."""
    result = ov.open_uri(r"C:\Users\test\My Vault")
    assert result == "obsidian://open?path=C:/Users/test/My%20Vault"


def test_open_uri_special_chars():
    """Percent-encode special characters but keep / and :."""
    result = ov.open_uri("/path/with spaces/and#hash")
    assert result == "obsidian://open?path=/path/with%20spaces/and%23hash"


def test_open_uri_keep_colon():
    """Keep colon unescaped (for drive letters on Windows)."""
    result = ov.open_uri("C:/Users/test/vault")
    assert result == "obsidian://open?path=C:/Users/test/vault"


# ============================================================
# main / CLI tests
# ============================================================

def test_main_status_only(tmp_path, monkeypatch):
    """Main with --status only (no --register)."""
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    config_file = str(tmp_path / "obsidian.json")
    with open(config_file, "w") as f:
        json.dump({"vaults": {"id1": {"path": vault_path, "ts": 1000, "open": True}}}, f)

    # Mock config_path to return our test config
    monkeypatch.setattr(ov, "config_path", lambda *a, **k: config_file)
    monkeypatch.setattr(ov, "find_obsidian", lambda *a, **k: "/fake/obsidian")
    monkeypatch.setattr(ov, "is_running", lambda *a, **k: False)

    # Run with HARNESS_LOGS and HARNESS_VAULT pointing to tmp_path
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))

    rc = ov.main(["--vault", vault_path, "--status"])
    assert rc == 0


def test_main_status_json(tmp_path, monkeypatch, capsys):
    """Main with --status --json outputs JSON."""
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    config_file = str(tmp_path / "obsidian.json")
    with open(config_file, "w") as f:
        json.dump({"vaults": {"id1": {"path": vault_path, "ts": 1000, "open": True}}}, f)

    monkeypatch.setattr(ov, "config_path", lambda *a, **k: config_file)
    monkeypatch.setattr(ov, "find_obsidian", lambda *a, **k: "/fake/obsidian")
    monkeypatch.setattr(ov, "is_running", lambda *a, **k: False)
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))

    rc = ov.main(["--vault", vault_path, "--status", "--json"])
    assert rc == 0

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["vault_registered"] is True
    assert output["obsidian_running"] is False
    assert output["config_path"] == config_file


def test_main_register_success(tmp_path, monkeypatch):
    """Main with --register succeeds."""
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    config_file = str(tmp_path / "obsidian.json")

    monkeypatch.setattr(ov, "config_path", lambda *a, **k: config_file)
    monkeypatch.setattr(ov, "find_obsidian", lambda *a, **k: "/fake/obsidian")
    monkeypatch.setattr(ov, "is_running", lambda *a, **k: False)
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))

    rc = ov.main(["--vault", vault_path, "--register"])
    assert rc == 0

    with open(config_file, "r") as f:
        data = json.load(f)
    assert len(data["vaults"]) == 1


def test_main_register_already_registered(tmp_path, monkeypatch, capsys):
    """Main with --register when already registered."""
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    config_file = str(tmp_path / "obsidian.json")
    with open(config_file, "w") as f:
        json.dump({"vaults": {"id1": {"path": vault_path, "ts": 1000, "open": True}}}, f)

    monkeypatch.setattr(ov, "config_path", lambda *a, **k: config_file)
    monkeypatch.setattr(ov, "find_obsidian", lambda *a, **k: "/fake/obsidian")
    monkeypatch.setattr(ov, "is_running", lambda *a, **k: False)
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))

    rc = ov.main(["--vault", vault_path, "--register"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "already registered" in captured.out


def test_main_register_obsidian_running(tmp_path, monkeypatch, capsys):
    """Main with --register when Obsidian is running exits with 4."""
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    config_file = str(tmp_path / "obsidian.json")

    monkeypatch.setattr(ov, "config_path", lambda *a, **k: config_file)
    monkeypatch.setattr(ov, "find_obsidian", lambda *a, **k: "/fake/obsidian")
    monkeypatch.setattr(ov, "is_running", lambda *a, **k: True)
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))

    rc = ov.main(["--vault", vault_path, "--register"])
    assert rc == 4

    captured = capsys.readouterr()
    assert "Obsidian is running" in captured.out

    # Config file should not be created/modified
    assert not Path(config_file).exists()


def test_main_register_error_invalid_json(tmp_path, monkeypatch):
    """Main with --register on invalid JSON returns 1."""
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    config_file = str(tmp_path / "obsidian.json")
    with open(config_file, "w") as f:
        f.write("{ invalid }")

    monkeypatch.setattr(ov, "config_path", lambda *a, **k: config_file)
    monkeypatch.setattr(ov, "find_obsidian", lambda *a, **k: "/fake/obsidian")
    monkeypatch.setattr(ov, "is_running", lambda *a, **k: False)
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))

    rc = ov.main(["--vault", vault_path, "--register"])
    assert rc == 1


def test_main_missing_vault_arg(monkeypatch):
    """Main without --vault returns 2."""
    monkeypatch.setenv("HARNESS_LOGS", "/tmp/logs")
    monkeypatch.setenv("HARNESS_VAULT", "/tmp/vault")

    rc = ov.main(["--register"])
    assert rc == 2


def test_main_register_json_output(tmp_path, monkeypatch, capsys):
    """Main with --register --json outputs JSON."""
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    config_file = str(tmp_path / "obsidian.json")

    monkeypatch.setattr(ov, "config_path", lambda *a, **k: config_file)
    monkeypatch.setattr(ov, "find_obsidian", lambda *a, **k: "/fake/obsidian")
    monkeypatch.setattr(ov, "is_running", lambda *a, **k: False)
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))

    rc = ov.main(["--vault", vault_path, "--register", "--json"])
    assert rc == 0

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["result"] == "registered"


# ============================================================
# _normalize_path tests (internal helper)
# ============================================================

def test_normalize_path_windows():
    """Normalize path on Windows."""
    result = ov._normalize_path(r"C:\Users\Test\My Vault", "windows")
    assert result == "c:/users/test/my vault"


def test_normalize_path_linux(monkeypatch):
    """Normalize path on Linux."""
    # Mock os.path.realpath to return the path unchanged
    monkeypatch.setattr(os.path, "realpath", lambda x: x)
    result = ov._normalize_path("/home/user/My Vault/", "linux")
    assert result == "/home/user/My Vault"


def test_normalize_path_forward_slashes():
    """Convert backslashes to forward slashes."""
    result = ov._normalize_path(r"C:\Users\Test\Vault", "windows")
    assert result == "c:/users/test/vault"


# ============================================================
# Edge cases
# ============================================================

def test_register_vault_vaults_not_dict(tmp_path):
    """Handle case where 'vaults' exists but is not a dict."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    with open(config_file, "w") as f:
        json.dump({"vaults": "not a dict"}, f)

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")
    assert result == "registered"

    with open(config_file, "r") as f:
        data = json.load(f)
    assert isinstance(data["vaults"], dict)
    assert "abcdef1234567890" in data["vaults"]


def test_register_vault_vault_info_not_dict(tmp_path):
    """Handle case where vault entry is not a dict."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    with open(config_file, "w") as f:
        json.dump({"vaults": {"id1": "not a dict"}}, f)

    result = ov.register_vault(config_file, vault_path, 1234567890000, "abcdef1234567890", "linux")
    assert result == "registered"

    with open(config_file, "r") as f:
        data = json.load(f)
    assert "abcdef1234567890" in data["vaults"]


def test_register_vault_atomic_write(tmp_path):
    """Write is atomic via temp file and os.replace."""
    config_file = str(tmp_path / "obsidian.json")
    vault_path = str(tmp_path / "myvault")
    Path(vault_path).mkdir()

    # First write
    ov.register_vault(config_file, vault_path, 1234567890000, "id1", "linux")

    # Second write should not leave temp files
    ov.register_vault(config_file, vault_path, 1234567891000, "id2", "linux")

    # Check no temp files left
    temp_files = list(tmp_path.glob("*.tmp"))
    assert len(temp_files) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])