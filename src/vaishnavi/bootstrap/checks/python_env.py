"""Doctor check: Python version window and "running inside the private environment" (stdlib)."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext

MIN_PYTHON = (3, 11)
MAX_TESTED = (3, 13)


def version_tuple() -> tuple[int, int]:
    """(major, minor) of the running interpreter (patchable in tests)."""
    return sys.version_info[0], sys.version_info[1]


def in_venv() -> bool:
    """True when a virtual environment is active."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


@dataclass
class PythonEnvCheck:
    """FAIL below 3.11 (E-SYS-001); WARN outside .venv or above the tested ceiling (E-SYS-011)."""

    key: str = "python_env"
    group: str = "software"

    def run(self, ctx: DoctorContext) -> CheckResult:
        """Inspect the interpreter."""
        major, minor = version_tuple()
        label = f"{major}.{minor}"
        if (major, minor) < MIN_PYTHON:
            return CheckResult.fail(ctx.t("doctor.python_env.too_old", version=label, need=f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"), error_code="E-SYS-001", detail=sys.executable)
        if not in_venv():
            return CheckResult.warn(ctx.t("doctor.python_env.not_venv", version=label), hint=ctx.t("doctor.python_env.not_venv_hint"), detail=sys.executable, version=label)
        if (major, minor) > MAX_TESTED:
            return CheckResult.warn(ctx.t("doctor.python_env.untested", version=label), error_code="E-SYS-011", detail=sys.executable, version=label)
        return CheckResult.ok(ctx.t("doctor.python_env.ok", version=label), version=label, executable=sys.executable)
