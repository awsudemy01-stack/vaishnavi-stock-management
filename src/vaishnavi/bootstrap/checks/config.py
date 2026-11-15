"""Doctor check: settings files load and validate; no API key inside a TOML file (stdlib)."""

from __future__ import annotations

from dataclasses import dataclass

from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext
from vaishnavi.core.errors import ConfigError, SecretsError
from vaishnavi.core.settings import Settings


@dataclass
class ConfigCheck:
    """FAIL on E-CFG-001 (invalid/missing key) or E-SEC-006 (secret in a file); the loaded settings replace ``ctx.settings``."""

    key: str = "config"
    group: str = "software"

    def run(self, ctx: DoctorContext) -> CheckResult:
        """Load from ``ctx.config_dir`` exactly as the app would."""
        try:
            settings = Settings.load(ctx.config_dir)
        except SecretsError as exc:
            return CheckResult.fail(ctx.t("doctor.config.secret_in_file"), error_code=exc.code, hint=exc.what_to_do(ctx.lang), detail=f"{exc.detail} in {exc.params.get('path', '')}")
        except ConfigError as exc:
            return CheckResult.fail(ctx.t("doctor.config.invalid", problem=exc.user_message(ctx.lang)), error_code=exc.code, hint=exc.what_to_do(ctx.lang), detail=exc.detail)
        ctx.settings = settings
        return CheckResult.ok(ctx.t("doctor.config.ok", shop=settings.shop.name), files=list(settings.source_files), provider=settings.ai.provider)
