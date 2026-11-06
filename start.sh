#!/usr/bin/env sh
# One command to start the setup interview (macOS / Linux):  sh start.sh   in a terminal opened in this folder.
cd "$(dirname "$0")" || exit 1
if [ ! -f questions.json ] || [ ! -f .claude/skills/setup/SKILL.md ]; then
    echo "This file has to stay in the kit folder, next to questions.json. Extract the whole folder first, then run this again."
    exit 1
fi
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
if ! command -v "$PY" >/dev/null 2>&1 || ! "$PY" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
    echo "Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ (macOS: or run: xcode-select --install; Linux: use your package manager), then run this again."
    exit 1
fi
if ! command -v claude >/dev/null 2>&1; then
    echo "Claude Code was not found. Install it from https://claude.com/claude-code, sign in once, then run this again."
    exit 1
fi
"$PY" finish_setup.py --trust-only "$(pwd)" >/dev/null 2>&1
echo "Starting the setup interview. Answer the questions; say ok to accept a suggestion."
exec claude "set me up"
