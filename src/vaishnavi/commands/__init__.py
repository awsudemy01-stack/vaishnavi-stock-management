"""``python -m vaishnavi <command>`` handlers — one module per command.

The registry lives here (not in ``__main__``) because ``python -m vaishnavi`` executes ``__main__`` under
the name ``__main__``; a command module importing ``vaishnavi.__main__`` would get a *second* copy with an
empty registry.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from vaishnavi.core.logging import LoggingHandle, setup_logging
from vaishnavi.core.settings import Settings
from vaishnavi.platform.paths import AppPaths


class Env:
    """Resolved settings, paths and language for a command."""

    def __init__(self, settings: Settings, paths: AppPaths, cfg_dir: Path, lang: str) -> None:
        self.settings = settings
        self.paths = paths
        self.config_dir = cfg_dir
        self.lang = lang


Handler = Callable[[argparse.Namespace, Env], int]
ArgAdder = Callable[[argparse.ArgumentParser], None]

COMMANDS: dict[str, tuple[Handler, str, ArgAdder | None]] = {}
"""name → (handler, help text, argument adder)."""


def register(name: str, help_text: str, add_args: ArgAdder | None = None) -> Callable[[Handler], Handler]:
    """Decorator used by command modules."""

    def deco(fn: Handler) -> Handler:
        COMMANDS[name] = (fn, help_text, add_args)
        return fn

    return deco


def read_stdin_secrets() -> dict[str, str]:
    """Installer → app secret hand-over (docs/07 §5): one JSON object ``{"secrets": {key: value}}`` on stdin.

    Secrets travel on stdin — never on the command line (visible in ``ps`` / Task Manager) and never in a file
    the app must later remember to delete. Interactive use (a TTY) is simply "no secrets".
    """
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logging.getLogger("vaishnavi.commands").warning("STDIN_NOT_JSON ignored (%d bytes)", len(raw))
        return {}
    secrets = data.get("secrets", {}) if isinstance(data, dict) else {}
    return {str(k): str(v) for k, v in secrets.items() if v is not None}


def start_file_logging(env: Env, *, console: bool = False) -> LoggingHandle | None:
    """Log to ``<primary>/logs`` for the duration of a command; console output (stderr) only when asked.

    The data root is **never** created here (``E-SYS-013`` rule): when it is missing — unplugged disk, or a
    bare PC with no installation — the command logs to stderr only. A failure here must not stop the
    command — the owner's action matters more than the diary.
    """
    try:
        if not env.paths.primary.exists():
            logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s", force=True)
            logging.getLogger("vaishnavi.commands").warning("DATA_ROOT_MISSING path=%s — logging to the console only code=E-SYS-013", env.paths.primary)
            return None
        cfg = env.config_dir / "logging.toml"
        return setup_logging(env.paths.logs, config_path=cfg if cfg.exists() else None, console=console, console_stream=sys.stderr)
    except Exception as exc:  # pragma: no cover - defensive
        logging.getLogger("vaishnavi.commands").warning("FILE_LOGGING_UNAVAILABLE %r", exc)
        return None
