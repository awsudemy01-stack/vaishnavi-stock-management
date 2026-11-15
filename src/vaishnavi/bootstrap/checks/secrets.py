"""Doctor check: OS key store available; leftover plain-text ``secrets.env`` (stdlib)."""

from __future__ import annotations

from dataclasses import dataclass

from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext
from vaishnavi.platform.secrets import keyring_available

SECRETS_ENV_FILE = "secrets.env"


@dataclass
class SecretsCheck:
    """WARN E-SEC-001 (file fallback in use) · WARN E-SEC-005 (plain-text secret file still present)."""

    key: str = "secrets"
    group: str = "security"

    def run(self, ctx: DoctorContext) -> CheckResult:
        """A leftover secrets file is the more urgent of the two."""
        leftover = ctx.config_dir / SECRETS_ENV_FILE
        if leftover.exists():
            from vaishnavi.core.errors import describe  # noqa: PLC0415

            return CheckResult.warn(ctx.t("doctor.secrets.leftover", path=str(leftover)), error_code="E-SEC-005", hint=describe("E-SEC-005", ctx.lang, {"path": str(leftover)})["what_to_do"], path=str(leftover))
        if not keyring_available():
            from vaishnavi.core.errors import describe  # noqa: PLC0415

            return CheckResult.warn(ctx.t("doctor.secrets.file_fallback"), error_code="E-SEC-001", hint=describe("E-SEC-001", ctx.lang, {})["what_to_do"], backend="file")
        return CheckResult.ok(ctx.t("doctor.secrets.ok"), backend="keyring")
