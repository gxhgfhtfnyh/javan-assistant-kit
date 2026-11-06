# -*- coding: utf-8 -*-
"""One-time, interactive setup for a NEW person running the politemall_*.py scripts (built 2026-11-06
for Javan: same polytechnic, same modules, same PoliteMall portal as Arthoven -- these scripts work
as-is once this is run; nothing in them needs a rewrite).

Run this yourself once:
    python politemall_setup.py

What it asks for and where it goes:
  - Your school email (e.g. 12345678@myrp.edu.sg)        -> logs/politemall_config.json (plain text, not a secret)
  - Your PoliteMall password                              -> Windows Credential Manager only, never written to a file
  - Your authenticator app's secret key (for 2FA)         -> Windows Credential Manager only, via politemall_totp.py setup
  - Where to save downloaded course files in your vault   -> logs/politemall_config.json (a path only, e.g. "D:\\MyVault\\School")
                                                              Leave blank to use a plain local folder instead (no Obsidian vault needed)
This never reads, stores or sends your password or secret key anywhere except Windows' own
Credential Manager on this PC. Safe to re-run any time to change your email or vault folder.
"""
import getpass
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import politemall_config as cfg  # noqa: E402

# Same modules confirmed for both Arthoven and Javan (same polytechnic, same year 2 program) --
# used only to RECOGNIZE existing loose notes by their module code, never assumed as the
# definitive list for someone else's course of study in general.
KNOWN_MODULE_CODES = ("A210", "A360", "A330", "A322")


def prompt_username(existing=None):
    default_note = f" (leave blank to keep: {existing})" if existing else ""
    email = input(f"Your school email{default_note}: ").strip()
    return email or existing


def prompt_vault_root(existing=None):
    default_note = f" (leave blank to keep: {existing})" if existing else " (leave blank for a plain local folder, no Obsidian vault)"
    raw = input(f"Folder to save course files into{default_note}: ").strip()
    return raw or existing


def find_loose_module_items(vault_root, school_root, module_codes=KNOWN_MODULE_CODES):
    """Pure (filesystem read-only). -> [(source_path, suggested_dest_path), ...] for every file or
    folder directly under vault_root whose name contains a known module code and which is NOT
    already inside school_root (so re-running this after a real move finds nothing left to do).
    Never guesses at anything outside vault_root's top level -- this only catches the exact shape
    Arthoven described (module notes sitting loose at the vault root), not a deep, unbounded scan
    of a vault it has never seen before."""
    vault_root, school_root = Path(vault_root), Path(school_root)
    moves = []
    if not vault_root.is_dir():
        return moves
    try:
        school_resolved = school_root.resolve()
    except OSError:
        school_resolved = school_root
    for item in sorted(vault_root.iterdir()):
        try:
            if item.resolve() == school_resolved or school_resolved in item.resolve().parents:
                continue
        except OSError:
            pass
        if item.name.startswith("."):
            continue
        if any(code in item.name.upper() for code in module_codes):
            moves.append((item, school_root / item.name))
    return moves


def preview_moves(moves):
    """Pure. One line per planned move, for the user to read and approve before anything touches disk."""
    if not moves:
        return "Nothing loose found at the vault root that looks like a module note/folder."
    lines = ["The following would move:"]
    for src, dest in moves:
        lines.append(f"  {src}  ->  {dest}")
    return "\n".join(lines)


def apply_moves(moves):
    """Side-effecting: only called after the user has seen preview_moves() and said yes. Skips (does
    not overwrite) anything already present at the destination, and reports what it actually did."""
    done, skipped = [], []
    for src, dest in moves:
        if dest.exists():
            skipped.append((src, dest, "destination already exists"))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
            done.append((src, dest))
        except OSError as e:
            skipped.append((src, dest, str(e)))
    return done, skipped


def build_config(username, vault_root, profile_dir=None, avatar_flag=None):
    """Pure. -> the dict to write as JSON. No secrets ever pass through this function."""
    return {
        "username": username,
        "home_url": cfg.DEFAULTS["home_url"],
        "profile_dir": profile_dir or cfg.resolve_profile_dir(username),
        "vault_school_root": vault_root or None,
        "avatar_nudge_flag": avatar_flag,
    }


def save_config(data, path=None):
    path = Path(path or cfg.CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def main():
    print("=== PoliteMall setup ===")
    print("This stores your password and 2FA secret in Windows Credential Manager only -- never in a file, never sent anywhere.\n")

    existing = cfg.load()
    username = prompt_username(existing.get("username"))
    if not username:
        print("No email given, nothing was saved. Run this again when you're ready.")
        return 1

    vault_root = prompt_vault_root(existing.get("vault_school_root"))
    data = build_config(username, vault_root)
    path = save_config(data)
    print(f"\nSaved: {path}")

    if vault_root:
        vault_base = input("\nPath to the TOP of your existing vault (leave blank to skip reorganizing old notes): ").strip()
        if vault_base:
            moves = find_loose_module_items(vault_base, vault_root)
            print("\n" + preview_moves(moves))
            if moves and input("\nMove these now? (yes/no): ").strip().lower() in ("yes", "y"):
                done, skipped = apply_moves(moves)
                print(f"Moved {len(done)}, skipped {len(skipped)}.")
                for src, dest, why in skipped:
                    print(f"  skipped {src} -> {dest}: {why}")

    print("\n--- Step 2: your PoliteMall password ---")
    import keyring
    password = getpass.getpass("Paste your PoliteMall password (hidden, nothing is shown): ").strip()
    if password:
        keyring.set_password("politemall", username, password)
        print("Stored in Windows Credential Manager.")
    else:
        print("Skipped (you can run this again later to add it).")

    print("\n--- Step 3: 2FA (authenticator app) ---")
    print("On the Microsoft security info page, add a new method -> Authenticator app -> 'I want to use a different authenticator app'")
    print("-> 'Can't scan image?' shows a secret key. Copy that (not the QR code, not your password).")
    import subprocess
    r = subprocess.run([sys.executable, str(HERE / "politemall_totp.py"), "setup"])
    if r.returncode != 0:
        print("2FA setup did not complete -- you can run `python politemall_totp.py setup` again any time.")

    print("\nAll set. Test it with: python login_politemall.py --headless")
    return 0


if __name__ == "__main__":
    sys.exit(main())
