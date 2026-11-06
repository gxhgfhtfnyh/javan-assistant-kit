# -*- coding: utf-8 -*-
"""Automatic game mode (OPT-IN): flips the game mode switch for you so you do not have to say "I am gaming now" and "I am done".

When a program from your list has been seen on TWO checks in a row while game mode is off, game mode is switched ON with --by auto. When game mode was switched on by
auto and none of your programs has been seen on THREE checks in a row, it is switched OFF. Game mode you turned on yourself is never switched off here.
It ONLY flips the switch (logs/game_mode.json, read by game_mode_guard_hook.py): it never applies the system cuts of `game_mode.py on --cuts`, whatever the settings.
Quiet: it sends nothing and prints one line.

Setup (nothing happens until you do it): list your games' program names in logs/game_auto_config.json, for example
    {"games": ["eldenring.exe", "VALORANT-Win64-Shipping.exe"], "on_after": 2, "off_after": 3}
Match is by program name, case-insensitive, with or without .exe. With no list this script does nothing. It ships as a DISABLED job in the job table: enable the job
"game_auto" (every 5 minutes) to turn the automatic switch on. Kill switch: the file logs/game_auto_off.txt.

    python game_auto.py             # one check
    python game_auto.py --dry-run   # show what it would do, change nothing

Running programs come from psutil when it is installed, else from `tasklist`. Windows only: elsewhere it prints one short line and exits 0. Always exits 0.
Environment: HARNESS_ROOT, HARNESS_LOGS. Pure core (unit tested): decide(state, game_seen, mode_on, by) -> (action, new_state), match_games, load_config."""
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

STATE_NAME = "game_auto_state.json"
CONFIG_NAME = "game_auto_config.json"
OFF_NAME = "game_auto_off.txt"
ON_AFTER = 2       # consecutive checks with a game before turning game mode on
OFF_AFTER = 3      # consecutive checks without a game before turning an auto game mode off
MAX_GAMES = 200


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


def _is_windows():
    return sys.platform == "win32"


def decide(state, game_seen, mode_on, by, on_after=ON_AFTER, off_after=OFF_AFTER):
    """-> (action, new_state); action is 'on', 'off' or None. State: {'seen': n, 'gone': n}."""
    state = state if isinstance(state, dict) else {}
    try:
        seen, gone = int(state.get("seen", 0)), int(state.get("gone", 0))
    except (TypeError, ValueError):
        seen, gone = 0, 0
    if game_seen:
        seen, gone = seen + 1, 0
    else:
        seen, gone = 0, gone + 1
    action = None
    if not mode_on and seen >= on_after:
        action = "on"
    elif mode_on and by == "auto" and gone >= off_after:
        action = "off"
    return action, {"seen": seen, "gone": gone}


def _bounded_int(value, default):
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 20 else default


def load_config(logs=None):
    """-> {"games": [...], "on_after": n, "off_after": n}. A missing or broken file gives no games. Never raises."""
    cfg = {"games": [], "on_after": ON_AFTER, "off_after": OFF_AFTER}
    try:
        raw = json.loads(((Path(logs) if logs else _logs()) / CONFIG_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    games = raw.get("games")
    if isinstance(games, list):
        cfg["games"] = [g.strip() for g in games if isinstance(g, str) and g.strip()][:MAX_GAMES]
    cfg["on_after"] = _bounded_int(raw.get("on_after"), ON_AFTER)
    cfg["off_after"] = _bounded_int(raw.get("off_after"), OFF_AFTER)
    return cfg


def _norm(name):
    n = str(name or "").strip().lower()
    return n[:-4] if n.endswith(".exe") else n


def match_games(running, games):
    """The configured game names that are in the set of running program names (comparison ignores case and .exe)."""
    have = {_norm(r) for r in running or ()}
    return sorted(g for g in games or () if _norm(g) in have)


def running_process_names():
    """The set of running program names, or None when they cannot be listed. psutil when installed, else `tasklist`."""
    try:
        import psutil
        names = set()
        for p in psutil.process_iter(["name"]):
            n = p.info.get("name")
            if n:
                names.add(n)
        return names
    except ImportError:
        pass
    except Exception:
        return None
    try:
        r = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                           stdin=subprocess.DEVNULL, creationflags=0x08000000 if os.name == "nt" else 0)
        if r.returncode != 0:
            return None
        return {row[0] for row in csv.reader(io.StringIO(r.stdout)) if row}
    except Exception:
        return None


def _load_state(logs):
    try:
        data = json.loads((Path(logs) / STATE_NAME).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(logs, state):
    p = Path(logs) / STATE_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def flip(action):
    """Switch game mode on (by auto, switch only) or off. Seam for tests."""
    import game_mode
    if action == "on":
        game_mode.enable(by="auto", cuts=False)
    elif action == "off":
        game_mode.disable()


def run_once(dry_run=False, logs=None):
    """One check. -> (message line). Never raises."""
    try:
        lg = Path(logs) if logs else _logs()
        if (lg / OFF_NAME).exists():
            return "game_auto: off switch present"
        cfg = load_config(lg)
        if not cfg["games"]:
            return "game_auto: no games listed (put their program names in logs/%s)" % CONFIG_NAME
        names = running_process_names()
        if names is None:
            return "game_auto: could not list running programs, nothing changed"
        import game_mode
        gm = game_mode.peek()
        action, new = decide(_load_state(lg), bool(match_games(names, cfg["games"])), bool(gm.get("on")), gm.get("by"), cfg["on_after"], cfg["off_after"])
        if not dry_run:
            _save_state(lg, new)
            if action:
                flip(action)
        return "game_auto: %s%s %s" % (action or "no change", " (dry run)" if dry_run and action else "", new)
    except Exception as e:
        return "game_auto: failed (%s)" % type(e).__name__


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not _is_windows():
        print("game_auto: Windows only, nothing to do here")
        return 0
    print(run_once(dry_run="--dry-run" in argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
