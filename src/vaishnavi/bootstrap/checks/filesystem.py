"""Doctor check: data folders exist and are writable; free disk space (stdlib)."""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext

GIB = 1024**3
WARN_FREE_GIB = 2.0
FAIL_FREE_GIB = 0.5


def writable(folder: Path) -> str | None:
    """None when a probe file can be written and removed; else the error text."""
    try:
        probe = folder / f".write-test-{uuid.uuid4().hex[:8]}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return str(exc)
    return None


@dataclass
class FilesystemCheck:
    """FAIL when the primary (data) location is missing/unwritable or nearly full; WARN when space is low."""

    key: str = "filesystem"
    group: str = "software"

    def run(self, ctx: DoctorContext) -> CheckResult:
        """Primary folders must exist (never re-created here — a missing folder means the disk is gone)."""
        paths = ctx.paths
        if not paths.primary.is_dir() or not paths.data.is_dir():
            return CheckResult.fail(ctx.t("doctor.filesystem.primary_missing", path=str(paths.primary)), error_code="E-SYS-013", hint=ctx.t("doctor.filesystem.primary_hint"), detail=f"primary={paths.primary} data_exists={paths.data.is_dir()}")
        for folder in paths.primary_folders():
            if not folder.is_dir():
                try:
                    folder.mkdir(parents=True, exist_ok=True)  # sub-folders inside an existing primary may be created
                except OSError as exc:
                    return CheckResult.fail(ctx.t("doctor.filesystem.not_writable", path=str(folder)), error_code="E-SYS-013", detail=str(exc))
            problem = writable(folder)
            if problem:
                return CheckResult.fail(ctx.t("doctor.filesystem.not_writable", path=str(folder)), error_code="E-SYS-013", hint=ctx.t("doctor.filesystem.primary_hint"), detail=problem)
        try:
            free_gib = round(shutil.disk_usage(paths.primary).free / GIB, 1)
        except OSError as exc:
            return CheckResult.fail(ctx.t("doctor.filesystem.primary_missing", path=str(paths.primary)), error_code="E-SYS-013", detail=str(exc))
        if free_gib < FAIL_FREE_GIB:
            return CheckResult.fail(ctx.t("doctor.filesystem.low_disk", free=free_gib), error_code="E-SYS-013", hint=ctx.t("doctor.filesystem.low_disk_hint"), free_gib=free_gib)
        if free_gib < WARN_FREE_GIB:
            return CheckResult.warn(ctx.t("doctor.filesystem.low_disk", free=free_gib), hint=ctx.t("doctor.filesystem.low_disk_hint"), free_gib=free_gib)
        return CheckResult.ok(ctx.t("doctor.filesystem.ok", free=free_gib, primary=str(paths.primary)), free_gib=free_gib, primary=str(paths.primary), secondary=str(paths.secondary))
