"""``python -m vaishnavi reset-owner-password`` — the app half of ``main.py --reset-owner-password`` (FR-A9).

Input is the installer's stdin document ``{"secrets": {"recovery_key": "...", "new_password": "..."}}``.
The installer has already proven OS administrator rights (``bootstrap/recovery.py``); a hand-over on a
*pipe* is therefore the elevation proof here — typing the command in a terminal gives no document and is
refused with ``E-AUTH-041``. Output: ``RESET_OK`` and the new recovery sheet as ``SECRET|`` lines, which
``handover.run_app`` shows to the owner but never logs.
"""

from __future__ import annotations

import argparse
import logging
import sys

from vaishnavi.commands import Env, read_stdin_secrets, register, start_file_logging
from vaishnavi.app_services import AppServices
from vaishnavi.core.errors import AuthError, VaishnaviError
from vaishnavi.core.logging import with_correlation
from vaishnavi.platform.elevation import is_admin

log = logging.getLogger("vaishnavi.commands.reset_owner_password")

SECRET_PREFIX = "SECRET|"


@register("reset-owner-password", "set a new Owner password with the 12-word recovery key (installer hand-over only)")
def run(ns: argparse.Namespace, env: Env) -> int:
    """See module docstring."""
    handle = start_file_logging(env)
    try:
        with with_correlation(user="installer", role="SYSTEM"):
            return _run(env)
    finally:
        if handle:
            handle.shutdown()


def _run(env: Env) -> int:
    piped = sys.stdin is not None and not sys.stdin.isatty()
    secrets = read_stdin_secrets()
    phrase, new_password = secrets.get("recovery_key", ""), secrets.get("new_password", "")
    elevated = piped or is_admin()
    if not env.paths.database.exists():
        sys.stderr.write("The database does not exist yet — run the installer first. (E-DB-002)\n")
        log.error("OWNER_RESET_REFUSED reason=no_database code=E-DB-002")
        return 2
    services = AppServices.build(env.settings, env.paths, env.config_dir)
    try:
        result = services.recovery.reset_owner_password(phrase, new_password, elevated=elevated)
    except AuthError as exc:
        log.warning("OWNER_RESET_REFUSED code=%s", exc.code)
        sys.stderr.write(f"{exc.user_message(env.lang)}\n{exc.what_to_do(env.lang)}  ({exc.code})\n")
        return 2
    except VaishnaviError as exc:
        log.error("OWNER_RESET_FAILED code=%s", exc.code)
        sys.stderr.write(f"{exc.user_message(env.lang)}  ({exc.code})\n")
        return 2
    sys.stdout.write(f"RESET_OK user={result.username}\n")
    if result.new_key is not None:
        for line in result.new_key.sheet(env.settings.shop.name).splitlines():
            sys.stdout.write(f"{SECRET_PREFIX}{line}\n")
    sys.stdout.flush()
    log.info("OWNER_PASSWORD_RESET_DONE user=%s new_key_issued=%s", result.username, result.new_key is not None)
    return 0
