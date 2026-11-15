"""Vaishnavi Store Manager - THE single entry point.

Run this file (or the Start Vaishnavi wrapper) on Windows, macOS or Ubuntu. It installs whatever is
missing, asks the few questions that need a human, checks the computer, and starts the shop software.
Documentation: docs/17-install-and-run.md.

This file is deliberately written in syntax that even very old Pythons can parse (and in plain ASCII,
so a Python 2 interpreter reaches the version check instead of failing on the encoding), so that a PC
with an outdated interpreter still gets a readable "please install Python 3.11+" message instead of a
crash. Everything real lives in the stdlib-only ``bootstrap/`` package.
"""

import os
import sys

MIN_PYTHON = (3, 11)


def _too_old_message(found):
    """Plain-language instructions when the interpreter is older than MIN_PYTHON."""
    lines = [
        "",
        "Vaishnavi Store Manager needs Python %d.%d or newer, but this computer started it with "
        "Python %s." % (MIN_PYTHON[0], MIN_PYTHON[1], found),
        "",
        "How to fix:",
        "  Windows : open the Microsoft Store or https://www.python.org/downloads/ , install Python 3.12,",
        "            tick 'Add python.exe to PATH', then double-click 'Start Vaishnavi.bat' again.",
        "  macOS   : install from https://www.python.org/downloads/ (or 'brew install python@3.12'),",
        "            then double-click 'Start Vaishnavi.command' again.",
        "  Ubuntu  : run  sudo apt install python3 python3-venv python3-pip  then run start-vaishnavi.sh again.",
        "",
        "Error code: E-SYS-001",
        "",
    ]
    return "\n".join(lines)


def _run():
    """Guard the interpreter version, then hand over to bootstrap.cli.main."""
    if sys.version_info < MIN_PYTHON:
        sys.stderr.write(_too_old_message("%d.%d" % (sys.version_info[0], sys.version_info[1])))
        return 2

    program_dir = os.path.dirname(os.path.abspath(__file__))
    if program_dir not in sys.path:
        sys.path.insert(0, program_dir)

    from bootstrap.cli import main as cli_main

    return cli_main(sys.argv[1:], program_dir=program_dir)


if __name__ == "__main__":
    sys.exit(_run())
