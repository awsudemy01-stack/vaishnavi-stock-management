# `bootstrap/` — root installer stages (stdlib only)

Runs **before** the virtual environment exists, driven by `main.py`.

Rules
- Import only the Python standard library. Never `import vaishnavi`, `nicegui`, `sqlalchemy`, …
- Anything that needs a third-party package is done by handing over to
  `.venv/…/python -m vaishnavi <cmd>` (see `handover.py`).
- Only this package and `src/vaishnavi/platform/` may test `sys.platform`.
- Not to be confused with `src/vaishnavi/bootstrap/` (doctor + startup, runs inside the venv).

Design: docs/07, docs/10 §2, docs/17.
