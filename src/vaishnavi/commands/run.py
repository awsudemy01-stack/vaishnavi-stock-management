"""``python -m vaishnavi run [--no-browser] [--port N]`` — the daily start (installer stage 12, autostart, watchdog).

Thin: logging to ``<primary>/logs`` first (so even a refusal is on file), then ``bootstrap.startup.run_app``.
Exit codes: 0 clean close · 2 refused (database damaged / another instance / files tampered) · 4 crash.
"""

from __future__ import annotations

import argparse
import logging
import sys

from vaishnavi.bootstrap.startup import run_app
from vaishnavi.commands import Env, register, start_file_logging

log = logging.getLogger("vaishnavi.commands.run")


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-browser", action="store_true", help="do not open the kiosk browser window")
    p.add_argument("--port", type=int, default=None, help="override the web port for this start")


@register("run", "start the application (web UI, scheduler, health endpoints)", _args)
def run(ns: argparse.Namespace, env: Env) -> int:
    """See module docstring."""
    if ns.port:
        env.settings.server.port = int(ns.port)
    handle = start_file_logging(env, console=sys.stderr.isatty())
    try:
        return run_app(settings=env.settings, paths=env.paths, config_dir=env.config_dir, open_browser=not ns.no_browser, lang=env.lang)
    finally:
        if handle:
            handle.shutdown()
