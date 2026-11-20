"""``python -m vaishnavi fresh-start`` — archive the database and start again with an empty catalogue.

Owner password required (typed at the prompt, or piped as ``{"secrets": {"owner_password": "…"}}`` by
``main.py --fresh-start``). Keeps users, PIN, settings, secrets, categories and assistant instructions;
clears products, stock, sales, shifts and everything derived (see ``services/fresh_start.py``).
Refuses while the application is running (``main.py --stop`` first, or use ``main.py --fresh-start``
which does that for you).
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from vaishnavi.app_services import AppServices
from vaishnavi.commands import Env, read_stdin_secrets, register, start_file_logging
from vaishnavi.core.errors import AuthError, VaishnaviError
from vaishnavi.core.logging import with_correlation
from vaishnavi.core.permissions import Role
from vaishnavi.services.fresh_start import fresh_start

log = logging.getLogger("vaishnavi.commands.fresh_start")


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--yes", action="store_true", help="do not ask for the typed confirmation word (the Owner password is still required)")


@register("fresh-start", "archive the database and start again with an empty catalogue (keeps users, settings, categories)", _args)
def run(ns: argparse.Namespace, env: Env) -> int:
    """See module docstring."""
    handle = start_file_logging(env)
    try:
        with with_correlation(user="owner-cli", role="SYSTEM"):
            return _run(ns, env)
    finally:
        if handle:
            handle.shutdown()


def _running(env: Env) -> bool:
    try:
        from vaishnavi.platform.watchdog import read_heartbeat  # noqa: PLC0415

        _at, pid = read_heartbeat(env.paths.state)
        return pid is not None and _pid_alive(pid)
    except Exception:  # noqa: BLE001 - heartbeat is advisory
        return False


def _pid_alive(pid: int) -> bool:
    import os  # noqa: PLC0415

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _run(ns: argparse.Namespace, env: Env) -> int:
    if not env.paths.database.exists():
        sys.stderr.write("There is no database yet — nothing to reset. (E-DB-002)\n")
        return 2
    if _running(env):
        sys.stderr.write("The shop application is running. Stop it first:  python main.py --stop   (or use  python main.py --fresh-start). (E-SYS-017)\n")
        log.warning("FRESH_START_REFUSED reason=app_running")
        return 2
    secrets = read_stdin_secrets()
    piped = bool(secrets)
    password = secrets.get("owner_password") or (getpass.getpass("Owner password: ") if sys.stdin and sys.stdin.isatty() else "")
    if not password:
        sys.stderr.write("Owner password is required. (E-AUTH-001)\n")
        return 2
    services = AppServices.build(env.settings, env.paths, env.config_dir)
    try:
        tiles = [t for t in services.users.login_tiles() if t[3] is Role.OWNER]
        if not tiles:
            sys.stderr.write("No Owner account found. (E-AUTH-005)\n")
            return 2
        if not services.users.reauthenticate(tiles[0][0], password):
            raise AuthError("E-AUTH-001", detail="owner password did not match")
    except AuthError as exc:
        log.warning("FRESH_START_REFUSED code=%s", exc.code)
        sys.stderr.write(f"{exc.user_message(env.lang)}  ({exc.code})\n")
        return 2
    if not ns.yes and not piped:
        sys.stdout.write("This clears ALL products, stock, bills and shifts (an archive copy is kept). Type  START FRESH  to continue: ")
        sys.stdout.flush()
        if (sys.stdin.readline() if sys.stdin else "").strip() != "START FRESH":
            sys.stdout.write("Cancelled — nothing changed.\n")
            return 3
    try:
        report = fresh_start(env.paths)
    except VaishnaviError as exc:
        log.error("FRESH_START_FAILED code=%s", exc.code)
        sys.stderr.write(f"{exc.user_message(env.lang)}  ({exc.code})\n")
        return 2
    except OSError as exc:
        log.error("FRESH_START_FAILED error=%s", exc)
        sys.stderr.write(f"Could not complete: {exc}\n")
        return 2
    kept = ", ".join(f"{t} {n}" for t, n in report.kept.items() if n)
    sys.stdout.write(f"FRESH_START_OK archive={report.archive_dir}\nKept: {kept}\nProducts, stock, bills and shifts are cleared. Start the app and add your items.\n")
    sys.stdout.flush()
    return 0
