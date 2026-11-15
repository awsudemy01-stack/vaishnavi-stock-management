"""``python -m vaishnavi <command>`` — the in-venv entry point the installer hands over to.

Commands are registered by the unit that implements them; an unknown or not-yet-available command
prints a plain line and exits 2 (``E-SYS-050``). Shared bootstrapping (settings + paths + logging) is
done once here so every command sees the same environment. Nothing in this module needs third-party
packages: ``doctor`` can run on a bare interpreter to tell the owner what is missing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from vaishnavi.commands import COMMANDS, Env
from vaishnavi.core.errors import ConfigError, VaishnaviError
from vaishnavi.core.settings import Settings
from vaishnavi.platform.paths import AppPaths, config_dir

log = logging.getLogger("vaishnavi.main")


def build_parser() -> argparse.ArgumentParser:
    """Top-level parser with one sub-parser per registered command (``--lang`` works before or after the command)."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--lang", choices=("en", "hi"), default=None, help="language of messages (default: settings shop.language)")
    common.add_argument("--config-dir", type=Path, default=None, help="folder with settings.toml / locations.toml (default: <program>/config)")
    # The sub-parser copies must not carry defaults, or they overwrite a value given before the command.
    common_sub = argparse.ArgumentParser(add_help=False)
    common_sub.add_argument("--lang", choices=("en", "hi"), default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common_sub.add_argument("--config-dir", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(prog="python -m vaishnavi", description="Vaishnavi Store Manager", parents=[common])
    sub = parser.add_subparsers(dest="command", metavar="command")
    for name, (_, help_text, add_args) in sorted(COMMANDS.items()):
        p = sub.add_parser(name, help=help_text, parents=[common_sub])
        if add_args:
            add_args(p)
    for planned in ("run", "init-db", "verify-chain", "backup", "selftest", "secret", "restore", "update", "reset-owner-password"):
        if planned not in COMMANDS:
            sub.add_parser(planned, help=f"{planned} (not available in this build)", parents=[common_sub])
    return parser


def resolve_env(ns: argparse.Namespace) -> Env:
    """Settings + locations → ``Env`` (a bad settings file is reported, not raised)."""
    cfg_dir = Path(ns.config_dir) if ns.config_dir else config_dir()
    try:
        settings = Settings.load(cfg_dir)
    except ConfigError as exc:
        log.error("SETTINGS_INVALID %s", exc)
        settings = Settings()  # the doctor's config check will report the problem in plain words
        settings.source_files.append(f"INVALID:{exc.code}")
    paths = AppPaths.from_locations(settings.locations.primary, settings.locations.secondary)
    lang = ns.lang or ("hi" if settings.shop.language == "hi" else "en")
    return Env(settings, paths, cfg_dir, lang)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch; never a traceback for the owner."""
    _import_commands()
    parser = build_parser()
    ns = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not ns.command:
        parser.print_help()
        return 2
    entry = COMMANDS.get(ns.command)
    if entry is None:
        sys.stderr.write(f"'{ns.command}' is not available in this version yet. (E-SYS-050)\n")
        return 2
    handler = entry[0]
    try:
        return int(handler(ns, resolve_env(ns)))
    except VaishnaviError as exc:
        log.error("COMMAND_FAILED command=%s %s", ns.command, exc)
        sys.stderr.write(f"{exc.user_message(ns.lang or 'en')}\n{exc.what_to_do(ns.lang or 'en')}  ({exc.code})\n")
        return 2
    except KeyboardInterrupt:
        return 3
    except Exception as exc:  # last line of defence
        log.exception("COMMAND_CRASH command=%s error=%r", ns.command, exc)
        sys.stderr.write(f"Unexpected error in '{ns.command}': {type(exc).__name__}. Details are in the log. (E-SYS-099)\n")
        return 4


def _import_commands() -> None:
    """Command modules register themselves on import (kept lazy so a broken optional dependency never blocks ``doctor``)."""
    import vaishnavi.commands.doctor  # noqa: F401, PLC0415

    for optional in ("run", "init_db", "verify_chain", "backup", "restore", "reset_owner_password", "selftest"):
        try:
            __import__(f"vaishnavi.commands.{optional}")
        except ImportError as exc:
            log.debug("command module %s not available: %s", optional, exc)


def command_names() -> list[str]:
    """Registered commands (tests)."""
    _import_commands()
    return sorted(COMMANDS)


if __name__ == "__main__":
    sys.exit(main())
