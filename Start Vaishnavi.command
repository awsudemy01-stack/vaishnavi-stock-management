#!/bin/bash
# Vaishnavi Store Manager - macOS start wrapper.
# Double-click this file in Finder (first time: right-click → Open). It finds Python, then runs
# main.py which installs/checks/starts everything. Documentation: docs/17-install-and-run.md
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1

PY=""
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then PY="python3"
fi

if [ -z "$PY" ]; then
  cat <<'MSG'

 Vaishnavi Store Manager needs Python 3.11 or newer, and no Python was found on this Mac.

 How to fix:
   1. Download the macOS installer from https://www.python.org/downloads/ and run it
      (or, if you use Homebrew:  brew install python@3.12 )
   2. Double-click "Start Vaishnavi.command" again.

 Error code: E-SYS-001

MSG
  read -r -p "Press Enter to close." _
  exit 2
fi

"$PY" main.py "$@"
CODE=$?
if [ "$CODE" -ne 0 ]; then
  echo
  echo " Setup ended with code $CODE. The messages above say what to do; the full log is in logs/install/ ."
  read -r -p "Press Enter to close." _
fi
exit "$CODE"
