"""Doctor check: required packages really import (stdlib; the list mirrors the installer's VERIFY_IMPORTS)."""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass

from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext

log = logging.getLogger("vaishnavi.bootstrap.checks.packages")

REQUIRED_MODULES: tuple[str, ...] = ("sqlalchemy", "alembic", "nicegui", "fastapi", "uvicorn", "cryptography", "keyring", "jinja2", "PIL", "qrcode", "barcode", "apscheduler", "httpx")


@dataclass
class PackagesCheck:
    """FAIL (E-SYS-002) when any required module fails to import."""

    key: str = "packages"
    group: str = "software"
    modules: tuple[str, ...] = REQUIRED_MODULES

    def run(self, ctx: DoctorContext) -> CheckResult:
        """Import each module (a broken wheel is caught here, not at the first sale)."""
        missing: list[str] = []
        for name in self.modules:
            try:
                importlib.import_module(name)
            except Exception as exc:  # ImportError, missing DLL, syntax error in a broken install
                log.warning("PACKAGE_IMPORT_FAILED module=%s error=%r", name, exc)
                missing.append(name)
        if missing:
            return CheckResult.fail(ctx.t("doctor.packages.missing", names=", ".join(missing)), error_code="E-SYS-002", hint=ctx.t("doctor.packages.missing_hint"), missing=missing)
        return CheckResult.ok(ctx.t("doctor.packages.ok", count=len(self.modules)), count=len(self.modules))
