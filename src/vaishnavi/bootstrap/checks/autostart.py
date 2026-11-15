"""Doctor check: the start-at-logon entry exists (WARN E-SYS-005 when not; never FAIL)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext
from vaishnavi.platform import autostart as platform_autostart


@dataclass
class AutostartCheck:
    """Uses ``platform.autostart.is_registered`` unless another probe is injected (tests)."""

    key: str = "autostart"
    group: str = "system"
    probe: Callable[[], bool | None] = field(default=platform_autostart.is_registered)

    def run(self, ctx: DoctorContext) -> CheckResult:
        """True → PASS · False → WARN · None → SKIP."""
        registered = self.probe()
        if registered is None:
            return CheckResult.skip(ctx.t("doctor.autostart.unknown"))
        if registered:
            return CheckResult.ok(ctx.t("doctor.autostart.ok"), registered=True)
        return CheckResult.warn(ctx.t("doctor.autostart.missing"), error_code="E-SYS-005", hint=ctx.t("doctor.autostart.missing_hint"), registered=False)
