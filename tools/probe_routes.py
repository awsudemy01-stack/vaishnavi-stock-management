"""Probe: does NiceGUI/FastAPI on *this* computer register a page whose function has a parameter named ``open``?

Run inside the app's venv, no database or server needed::

    .venv/bin/python tools/probe_routes.py          # macOS / Linux
    .venv\\Scripts\\python tools\\probe_routes.py    # Windows

Live run 3 (16-Nov-2026): ``/shifts`` was the only page missing from FastAPI's route table on the shop PC
(nicegui 4.0.0 · fastapi 0.163.1 · starlette 1.10.0) although its decorator ran; it is also the only page
whose function takes a query parameter called ``open``. This script registers five variants and prints which
of them FastAPI actually knows.
"""

from __future__ import annotations

import importlib.metadata as md
import sys


def main() -> int:
    from nicegui import app, ui

    for dist in ("nicegui", "fastapi", "starlette", "uvicorn", "pydantic"):
        try:
            print(f"{dist:10} {md.version(dist)}")
        except md.PackageNotFoundError:
            print(f"{dist:10} (not installed)")
    print(f"python     {sys.version.split()[0]}")

    @ui.page("/probe/open-param")
    def _open_param(open: int = 0) -> None:  # noqa: A002 - this is the suspect
        ui.label(str(open))

    @ui.page("/probe/other-param")
    def _other_param(x: int = 0) -> None:
        ui.label(str(x))

    @ui.page("/probe/no-param")
    def _no_param() -> None:
        ui.label("ok")

    @ui.page("/probe/open-str")
    def _open_str(open: str = "") -> None:  # noqa: A002
        ui.label(open)

    @ui.page("/probe/open-renamed")
    def _open_renamed(open_drawer: int = 0) -> None:
        ui.label(str(open_drawer))

    paths = sorted(str(p) for p in (getattr(r, "path", None) for r in app.routes) if p and str(p).startswith("/probe/"))
    expected = ["/probe/no-param", "/probe/open-param", "/probe/open-renamed", "/probe/open-str", "/probe/other-param"]
    print("\nregistered:", ", ".join(paths) or "-")
    missing = [p for p in expected if p not in paths]
    print("missing:   ", ", ".join(missing) or "none")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
