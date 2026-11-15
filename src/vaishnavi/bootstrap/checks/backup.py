"""Doctor check: secondary location reachable, last backup age and verification; ``fix`` makes a snapshot (stdlib)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext
from vaishnavi.core.errors import BackupError
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.services.backup import BackupService
from vaishnavi.services.ledger import LedgerService

log = logging.getLogger("vaishnavi.bootstrap.checks.backup")

STALE_HOURS = 24.0


def _service(ctx: DoctorContext) -> BackupService:
    svc = ctx.runtime.get("backup")
    if svc is None:
        ledger = ctx.runtime.get("ledger") or LedgerService(SqliteLedgerStore())
        svc = BackupService(ctx.paths, ctx.settings.backup, ledger, same_device_accepted=ctx.settings.locations.same_device)
        ctx.runtime["backup"] = svc
    return svc


def _age_text(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)} min"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} days"


@dataclass
class BackupCheck:
    """WARN: never backed up, stale (> 24 h), or the backup drive is unreachable (E-BKP-001). Never FAIL — selling must not stop."""

    key: str = "backup"
    group: str = "backup"

    def run(self, ctx: DoctorContext) -> CheckResult:
        """Look at the newest snapshot and the drive."""
        if not ctx.paths.database.exists():
            return CheckResult.skip("no database")
        svc = _service(ctx)
        ready, reason = svc.secondary_ready()
        queued = svc.queued_count()
        age = svc.last_backup_age_hours()
        data = {"secondary_ready": ready, "queued": queued, "age_hours": age, "secondary": str(ctx.paths.secondary)}
        if not ready:
            return CheckResult.warn(ctx.t("doctor.backup.secondary_down", queued=queued), error_code="E-BKP-001", hint=_hint(ctx, "E-BKP-001"), detail=reason, **data)
        if age is None:
            return CheckResult.warn(ctx.t("doctor.backup.never"), hint=ctx.t("doctor.backup.never_hint"), **data)
        if age > STALE_HOURS:
            return CheckResult.warn(ctx.t("doctor.backup.stale", age=_age_text(age)), hint=ctx.t("doctor.backup.stale_hint"), **data)
        return CheckResult.ok(ctx.t("doctor.backup.ok", age=_age_text(age), where=str(ctx.paths.secondary)), **data)

    def fix(self, ctx: DoctorContext) -> CheckResult | None:
        """Take a snapshot now (queues locally when the drive is away)."""
        if not ctx.paths.database.exists():
            return None
        try:
            snap = _service(ctx).snapshot("doctor")
        except BackupError as exc:
            log.warning("DOCTOR_FIX backup failed: %s", exc)
            return CheckResult.warn(exc.user_message(ctx.lang), error_code=exc.code, hint=exc.what_to_do(ctx.lang), detail=exc.detail)
        if snap.queued:
            return self.run(ctx)
        return CheckResult.ok(ctx.t("doctor.backup.fixed"), backup_id=snap.backup_id, path=snap.path)


def _hint(ctx: DoctorContext, code: str) -> str:
    from vaishnavi.core.errors import describe  # noqa: PLC0415

    return describe(code, ctx.lang, {"path": str(ctx.paths.secondary)})["what_to_do"]
