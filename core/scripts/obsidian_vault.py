#!/usr/bin/env python3
"""
Obsidian Vault Registration Module

Finds Obsidian installation, locates its config file, and registers a folder
as a vault by editing obsidian.json.

Usage:
    python obsidian_vault.py --vault <path> [--register] [--status] [--json]

Functions:
    find_obsidian(platform, env, home, exists, runner) -> str | None
    config_path(platform, env, home, exists) -> str
    is_running(platform, runner) -> bool
    register_vault(config_file, vault_path, now_ms, rand_hex) -> str
    open_uri(vault_path) -> str
    main(argv) -> int
"""

import json
import os
import platform as platform_module
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote


def system_runner(args: list[str]) -> tuple[int, str]:
    """Run a program and give back (exit code, its output); (127, "") when it does not exist, (124, "") when it hangs. No console window on Windows."""
    import subprocess
    try:
        r = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=20, stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return r.returncode, r.stdout or ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""


def find_obsidian(
    platform: str,
    env: dict[str, str],
    home: str,
    exists: Callable[[str], bool],
    runner: Callable[[list[str]], tuple[int, str]],
    which: Callable[[str], str | None] = None,
) -> str | None:
    """
    Find Obsidian executable path.

    Args:
        platform: 'windows', 'darwin', or 'linux'
        env: environment variables dict
        home: home directory path
        exists: function to check if path exists
        runner: function to run commands, returns (returncode, stdout)
        which: function to find executable on PATH (for Linux)

    Returns:
        Path to Obsidian executable, or special strings for flatpak/snap,
        or None if not found.
    """
    if platform == "windows":
        localappdata = env.get("LOCALAPPDATA", "")
        programfiles = env.get("ProgramFiles", "")
        programfiles_x86 = env.get("ProgramFiles(x86)", "")

        candidates = []
        if localappdata:
            candidates.append(os.path.join(localappdata, "Programs", "Obsidian", "Obsidian.exe"))
            candidates.append(os.path.join(localappdata, "Obsidian", "Obsidian.exe"))
        if programfiles:
            candidates.append(os.path.join(programfiles, "Obsidian", "Obsidian.exe"))
        if programfiles_x86:
            candidates.append(os.path.join(programfiles_x86, "Obsidian", "Obsidian.exe"))

        for path in candidates:
            if exists(path):
                return path
        return None

    elif platform == "darwin":
        candidates = [
            "/Applications/Obsidian.app",
            os.path.join(home, "Applications", "Obsidian.app"),
        ]
        for path in candidates:
            if exists(path):
                return path
        return None

    elif platform == "linux":
        # Check PATH first
        if which:
            obsidian_path = which("obsidian")
            if obsidian_path:
                return obsidian_path

        # Check flatpak
        rc, stdout = runner(["flatpak", "list", "--app"])
        if rc == 0 and "md.obsidian.Obsidian" in stdout:
            return "flatpak:md.obsidian.Obsidian"

        # Check AppImage in ~/Applications or ~/Downloads
        for subdir in ("Applications", "Downloads"):
            app_dir = os.path.join(home, subdir)
            if exists(app_dir):
                try:
                    for entry in os.listdir(app_dir):
                        if entry.startswith("Obsidian") and entry.endswith(".AppImage"):
                            full_path = os.path.join(app_dir, entry)
                            if exists(full_path):
                                return full_path
                except OSError:
                    pass

        # Check snap
        rc, stdout = runner(["snap", "list", "obsidian"])
        if rc == 0 and "obsidian" in stdout.lower():
            return "snap:obsidian"

        return None

    return None


def config_path(
    platform: str,
    env: dict[str, str],
    home: str,
    exists: Callable[[str], bool],
) -> str:
    """
    Get the path to obsidian.json config file.

    Args:
        platform: 'windows', 'darwin', or 'linux'
        env: environment variables dict
        home: home directory path
        exists: function to check if path exists

    Returns:
        Path to obsidian.json
    """
    if platform == "windows":
        appdata = env.get("APPDATA", "")
        if appdata:
            return os.path.join(appdata, "obsidian", "obsidian.json")
        # Fallback
        return os.path.join(home, "AppData", "Roaming", "obsidian", "obsidian.json")

    elif platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "obsidian", "obsidian.json")

    elif platform == "linux":
        # Check flatpak config first
        flatpak_config = os.path.join(
            home, ".var", "app", "md.obsidian.Obsidian", "config", "obsidian", "obsidian.json"
        )
        if exists(flatpak_config):
            return flatpak_config

        # Standard XDG config
        xdg_config = env.get("XDG_CONFIG_HOME")
        if xdg_config:
            return os.path.join(xdg_config, "obsidian", "obsidian.json")

        return os.path.join(home, ".config", "obsidian", "obsidian.json")

    # Fallback
    return os.path.join(home, ".config", "obsidian", "obsidian.json")


def is_running(platform: str, runner: Callable[[list[str]], tuple[int, str]]) -> bool:
    """
    Check if Obsidian is currently running.

    Args:
        platform: 'windows', 'darwin', or 'linux'
        runner: function to run commands, returns (returncode, stdout)

    Returns:
        True if Obsidian process is found
    """
    if platform == "windows":
        rc, stdout = runner(["tasklist", "/FI", "IMAGENAME eq Obsidian.exe"])
        return rc == 0 and "Obsidian.exe" in stdout
    else:
        # Try exact match first (Linux), then case-insensitive (macOS)
        rc, stdout = runner(["pgrep", "-x", "Obsidian"])
        if rc == 0 and stdout.strip():
            return True
        rc, stdout = runner(["pgrep", "-xi", "obsidian"])
        return rc == 0 and bool(stdout.strip())

    return False


def _normalize_path(path: str, platform: str) -> str:
    """Normalize path for comparison."""
    # Resolve path
    try:
        resolved = os.path.realpath(path)
    except OSError:
        resolved = path

    # Forward slashes
    normalized = resolved.replace("\\", "/")

    # Lowercase on Windows
    if platform == "windows":
        normalized = normalized.lower()

    # Remove trailing slash
    normalized = normalized.rstrip("/")

    return normalized


def register_vault(
    config_file: str,
    vault_path: str,
    now_ms: int,
    rand_hex: str,
    platform: str = "linux",
    exists: Callable[[str], bool] = os.path.exists,
) -> str:
    """
    Register a vault in obsidian.json.

    Args:
        config_file: Path to obsidian.json
        vault_path: Path to vault folder to register
        now_ms: Current timestamp in milliseconds
        rand_hex: 16-character hex string for new vault ID
        platform: 'windows', 'darwin', or 'linux' (for path normalization)
        exists: function to check if path exists

    Returns:
        'registered', 'already registered', or raises ValueError on error
    """
    # Read existing config
    data: dict[str, Any] = {"vaults": {}}
    if exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    parsed = json.loads(content)
                    if not isinstance(parsed, dict):
                        raise ValueError("Config file top level is not an object")
                    data = parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in config file: {e}")

    # Ensure vaults dict exists
    if "vaults" not in data or not isinstance(data["vaults"], dict):
        data["vaults"] = {}

    # Normalize the target vault path for comparison
    vault_path_normalized = _normalize_path(vault_path, platform)

    # Check if already registered (compare normalized paths)
    for vault_id, vault_info in data["vaults"].items():
        if not isinstance(vault_info, dict):
            continue
        existing_path = vault_info.get("path", "")
        if existing_path:
            existing_normalized = _normalize_path(existing_path, platform)
            if existing_normalized == vault_path_normalized:
                # Already registered: touch the file only when the open flag has to change
                if vault_info.get("open") is not True:
                    vault_info["open"] = True
                    _write_config(config_file, data, exists)
                return "already registered"

    # Not registered - add new entry
    # Use platform's usual path separators for storage
    stored_path = vault_path
    if platform == "windows":
        stored_path = vault_path.replace("/", "\\")

    new_vault = {
        "path": stored_path,
        "ts": now_ms,
        "open": True,
    }
    data["vaults"][rand_hex] = new_vault

    # Write back
    _write_config(config_file, data, exists)
    return "registered"


def _write_config(
    config_file: str,
    data: dict[str, Any],
    exists: Callable[[str], bool],
) -> None:
    """Write config file with backup and atomic replace."""
    # Create parent directory if needed
    parent_dir = os.path.dirname(config_file)
    if parent_dir and not exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)

    # Create backup if file exists
    if exists(config_file):
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        backup_name = f"{config_file}.bak_starter_kit.{timestamp}"
        # Never overwrite an older backup
        if not exists(backup_name):
            try:
                import shutil
                shutil.copy2(config_file, backup_name)
            except OSError:
                pass  # Best effort

    # Write through temp file and atomic replace
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent_dir,
        delete=False,
        suffix=".tmp",
    ) as tf:
        json.dump(data, tf, indent=2, ensure_ascii=False)
        tf.write("\n")
        temp_name = tf.name

    try:
        os.replace(temp_name, config_file)
    except OSError:
        # Clean up temp file on failure
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def open_uri(vault_path: str) -> str:
    """
    Generate obsidian:// URI to open a vault.

    Args:
        vault_path: Path to vault folder

    Returns:
        obsidian:// URI with percent-encoded path (keeping / and : unescaped)
    """
    # Normalize to forward slashes
    normalized = vault_path.replace("\\", "/")
    # Percent-encode but keep / and : unescaped
    encoded = quote(normalized, safe="/:")
    return f"obsidian://open?path={encoded}"


def main(argv: list[str]) -> int:
    """
    Main CLI entry point.

    Args:
        argv: Command line arguments (sys.argv[1:])

    Returns:
        Exit code: 0 success, 1 error, 2 usage error, 4 Obsidian running
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Find Obsidian and register vaults",
        add_help=False,
    )
    parser.add_argument("--vault", required=True, help="Path to vault folder")
    parser.add_argument("--register", action="store_true", help="Register the vault")
    parser.add_argument("--status", action="store_true", help="Show status only")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--help", action="help", help="Show this help")

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return 2

    # Get platform
    sys_platform = sys.platform
    if sys_platform.startswith("win"):
        platform = "windows"
    elif sys_platform == "darwin":
        platform = "darwin"
    else:
        platform = "linux"

    env = dict(os.environ)
    home = str(Path.home())
    args.vault = os.path.abspath(args.vault)                          # Obsidian must be given an absolute path

    # Find Obsidian
    obsidian_path = find_obsidian(platform, env, home, os.path.exists, system_runner, shutil.which)

    # Get config path
    cfg_path = config_path(platform, env, home, os.path.exists)

    # Check if vault is registered
    registered = False
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "vaults" in data:
                    vault_path_normalized = _normalize_path(args.vault, platform)
                    for vault_info in data["vaults"].values():
                        if isinstance(vault_info, dict):
                            existing_path = vault_info.get("path", "")
                            if existing_path:
                                existing_normalized = _normalize_path(existing_path, platform)
                                if existing_normalized == vault_path_normalized:
                                    registered = True
                                    break
        except (json.JSONDecodeError, OSError):
            pass

    # Check if running
    running = is_running(platform, system_runner)

    if not args.register:
        # Report only
        if args.json:
            result = {
                "obsidian_path": obsidian_path,
                "config_path": cfg_path,
                "vault_registered": registered,
                "obsidian_running": running,
            }
            print(json.dumps(result, indent=2))
        else:
            print(f"Obsidian: {obsidian_path or 'not found'}")
            print(f"Config: {cfg_path}")
            print(f"Vault registered: {'yes' if registered else 'no'}")
            print(f"Obsidian running: {'yes' if running else 'no'}")
        return 0

    # Register mode
    if running:
        print("Obsidian is running: close it and run this again so it does not overwrite the change")
        return 4

    # Register the vault
    now_ms = int(time.time() * 1000)
    import secrets
    rand_hex = secrets.token_hex(8)  # 16 hex chars

    try:
        result = register_vault(cfg_path, args.vault, now_ms, rand_hex, platform, os.path.exists)
        if args.json:
            print(json.dumps({"result": result}))
        else:
            print(result)
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    import shutil
    sys.exit(main(sys.argv[1:]))