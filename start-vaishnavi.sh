#!/bin/bash
# Vaishnavi Store Manager - Ubuntu / Debian start wrapper.
# Run:  ./start-vaishnavi.sh   (or double-click → "Run in Terminal"). It finds Python, then runs
# main.py which installs/checks/starts everything. Documentation: docs/17-install-and-run.md
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1

PY=""
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then PY="python3"
fi

if [ -z "$PY" ]; then
  cat <<'MSG'

 Vaishnavi Store Manager needs Python 3.11 or newer, and no Python was found on this computer.

 How to fix (Ubuntu / Debian), copy this line into a terminal:
     sudo apt install python3 python3-venv python3-pip
 then run ./start-vaishnavi.sh again.

 Error code: E-SYS-001

MSG
  exit 2
fi

"$PY" main.py "$@"
CODE=$?
if [ "$CODE" -ne 0 ] && [ -t 1 ]; then
  echo
  echo " Setup ended with code $CODE. The messages above say what to do; the full log is in logs/install/ ."
fi
exit "$CODE"
