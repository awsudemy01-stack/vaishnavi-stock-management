"""Stdlib-only tier: installer, money, tax, measure, EAN-13, hashing, invoice formatting, guardrail classifier, forecast methods.

Runs with the bare system Python: ``python -m unittest discover -s tests/core -t .``
"""

import logging
import sys
from pathlib import Path

# The core tier runs with the bare system Python (nothing installed): make `import vaishnavi` work
# straight from the source tree. Harmless when the package is installed in the venv.
_SRC = str(Path(__file__).resolve().parents[2] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Without any handler Python's "last resort" handler prints WARNING+ records to stderr, which makes
# test output noisy. Tests that need to inspect log output attach their own handlers.
logging.getLogger().addHandler(logging.NullHandler())
