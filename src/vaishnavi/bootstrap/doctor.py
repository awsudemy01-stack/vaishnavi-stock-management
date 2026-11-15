"""Pre-flight doctor: check protocol, runner, reporters and exit codes (docs/07 §5; stdlib only).

Every check answers with a :class:`CheckResult` — status, one plain-language line, a "what to do"
hint, a technical detail (log only), an error code, structured data and elapsed ms. The runner never
raises: a crashing check is a ``FAIL`` with ``E-SYS-099`` and the traceback in the log. With ``--fix``
a check that offers ``fix()`` is asked to repair a non-PASS result (safe actions only: create the
database, make a backup, set WAL…). Exit codes: 0 PASS · 1 WARN · 2 FAIL (SKIP counts as PASS).

The report is rendered as plain text in English or Hindi (``render_text``) and as JSON
(``render_json`` — the same object ``/api/v1/system/doctor`` returns). Checks live one group per
module in ``vaishnavi/bootstrap/checks/``; :func:`default_checks` lists them in the documented order.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from vaishnavi.core.clock import format_ist, iso_utc, now_utc
from vaishnavi.core.i18n import Texts
from vaishnavi.core.ids import new_ulid
from vaishnavi.core.settings import Settings
from vaishnavi.platform.paths import AppPaths

log = logging.getLogger("vaishnavi.bootstrap.doctor")


class Status(str, Enum):
    """Outcome of one check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


_SEVERITY = {Status.SKIP: 0, Status.PASS: 0, Status.WARN: 1, Status.FAIL: 2}


@dataclass(frozen=True)
class CheckResult:
    """What a check reports."""

    status: Status
    message: str = ""
    hint: str = ""
    detail: str = ""
    error_code: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    fix_applied: bool = False
    key: str = ""
    group: str = ""

    @classmethod
    def ok(cls, message: str = "", **data: Any) -> CheckResult:
        """PASS."""
        return cls(Status.PASS, message, data=data)

    @classmethod
    def warn(cls, message: str, *, hint: str = "", error_code: str = "", detail: str = "", **data: Any) -> CheckResult:
        """WARN — the app may start."""
        return cls(Status.WARN, message, hint, detail, error_code, data)

    @classmethod
    def fail(cls, message: str, *, error_code: str, hint: str = "", detail: str = "", **data: Any) -> CheckResult:
        """FAIL — the app must not start."""
        return cls(Status.FAIL, message, hint, detail, error_code, data)

    @classmethod
    def skip(cls, message: str = "", **data: Any) -> CheckResult:
        """Not applicable here."""
        return cls(Status.SKIP, message, data=data)

    def stamped(self, *, key: str, group: str, elapsed_ms: int, fix_applied: bool = False) -> CheckResult:
        """Copy with runner bookkeeping filled in."""
        return CheckResult(self.status, self.message, self.hint, self.detail, self.error_code, dict(self.data), elapsed_ms, fix_applied, key, group)


@dataclass
class DoctorContext:
    """What checks may look at. ``runtime`` carries live objects (services) and flags such as ``app_running``."""

    settings: Settings
    paths: AppPaths
    config_dir: Path
    lang: str = "en"
    fix: bool = False
    runtime: dict[str, Any] = field(default_factory=dict)
    texts: Texts | None = None

    def t(self, key: str, **params: Any) -> str:
        """Owner-facing text in the report language."""
        if self.texts is None:
            self.texts = Texts.load(self.lang)
        return self.texts.t(key, **params)


@runtime_checkable
class Check(Protocol):
    """One doctor check. ``fix(ctx) -> CheckResult | None`` is optional."""

    key: str
    group: str

    def run(self, ctx: DoctorContext) -> CheckResult:  # pragma: no cover - protocol
        """Inspect; never raise for an expected problem."""
        ...


@dataclass(frozen=True)
class DoctorReport:
    """A complete pass."""

    ref: str
    at: str
    results: tuple[CheckResult, ...]
    duration_ms: int

    @property
    def worst(self) -> Status:
        """Highest severity."""
        worst = Status.PASS
        for r in self.results:
            if _SEVERITY[r.status] > _SEVERITY[worst]:
                worst = r.status
        return worst

    @property
    def exit_code(self) -> int:
        """0 PASS/SKIP · 1 WARN · 2 FAIL."""
        return _SEVERITY[self.worst]

    @property
    def warnings(self) -> list[CheckResult]:
        """WARN results."""
        return [r for r in self.results if r.status is Status.WARN]

    @property
    def failures(self) -> list[CheckResult]:
        """FAIL results."""
        return [r for r in self.results if r.status is Status.FAIL]


# ----------------------------------------------------------------------------- runner
def run_doctor(checks: Sequence[Check], ctx: DoctorContext) -> DoctorReport:
    """Run every check in order (no early stop — the owner wants the whole picture)."""
    ref = new_ulid()
    started = time.perf_counter()
    at = iso_utc()
    log.info("DOCTOR_START ref=%s checks=%d fix=%s lang=%s", ref, len(checks), ctx.fix, ctx.lang)
    results: list[CheckResult] = []
    for check in checks:
        t0 = time.perf_counter()
        fix_applied = False
        try:
            result = check.run(ctx)
            if ctx.fix and result.status in (Status.WARN, Status.FAIL) and hasattr(check, "fix"):
                log.info("DOCTOR_FIX_ATTEMPT check=%s code=%s", check.key, result.error_code or "-")
                fixed = check.fix(ctx)  # type: ignore[attr-defined]
                if fixed is not None:
                    result, fix_applied = fixed, True
        except Exception as exc:  # a broken check must not hide the other results
            log.error("DOCTOR_CHECK_CRASH check=%s error=%r\n%s", check.key, exc, traceback.format_exc())
            result = CheckResult.fail("internal error in this check", error_code="E-SYS-099", detail=repr(exc))
        elapsed = int((time.perf_counter() - t0) * 1000)
        stamped = result.stamped(key=check.key, group=check.group, elapsed_ms=elapsed, fix_applied=fix_applied)
        results.append(stamped)
        log.log(
            logging.WARNING if stamped.status in (Status.WARN, Status.FAIL) else logging.INFO,
            "DOCTOR_CHECK key=%s status=%s code=%s ms=%d msg=%s detail=%s fixed=%s",
            stamped.key, stamped.status.value, stamped.error_code or "-", elapsed, stamped.message, stamped.detail or "-", fix_applied,
        )
    report = DoctorReport(ref, at, tuple(results), int((time.perf_counter() - started) * 1000))
    log.info("DOCTOR_END ref=%s exit=%d worst=%s ms=%d", ref, report.exit_code, report.worst.value, report.duration_ms)
    return report


# ----------------------------------------------------------------------------- reporters
_MARK = {Status.PASS: "✔", Status.WARN: "!", Status.FAIL: "✖", Status.SKIP: "–"}
_MARK_ASCII = {Status.PASS: "[OK]", Status.WARN: "[! ]", Status.FAIL: "[X ]", Status.SKIP: "[- ]"}


def render_text(report: DoctorReport, texts: Texts, *, unicode: bool = True, details_url: str = "") -> str:
    """The owner-facing report (docs/07 §5 example)."""
    marks = _MARK if unicode else _MARK_ASCII
    rule = ("─" if unicode else "-") * 90
    lines = [texts.t("doctor.header", when=format_ist(now_utc()), ref=report.ref), rule]
    for r in report.results:
        title = texts.t(f"doctor.title.{r.key}")
        text = r.message
        if r.fix_applied:
            text = f"{text} ({texts.t('doctor.fixed')})"
        code = f"  ({r.error_code})" if r.error_code and r.status in (Status.WARN, Status.FAIL) else ""
        lines.append(f" {marks[r.status]:<4} {title:<22} {text}{code}".rstrip() + f"{'':>4}{r.elapsed_ms:>6} ms")
        if r.hint and r.status in (Status.WARN, Status.FAIL):
            lines.append(("      → " if unicode else "      -> ") + r.hint)
    lines.append(rule)
    if report.worst is Status.FAIL:
        lines.append(" " + texts.t("doctor.result_fail", count=len(report.failures)))
    elif report.worst is Status.WARN:
        lines.append(" " + texts.t("doctor.result_warn", count=len(report.warnings)))
    else:
        lines.append(" " + texts.t("doctor.result_ok"))
    if details_url:
        lines.append(" " + texts.t("doctor.details", where=f"{details_url}?ref={report.ref}"))
    return "\n".join(lines)


def render_json(report: DoctorReport) -> str:
    """Machine-readable form (API, Settings → System, tests)."""
    doc = {
        "ref": report.ref,
        "at": report.at,
        "duration_ms": report.duration_ms,
        "worst": report.worst.value,
        "exit_code": report.exit_code,
        "checks": [
            {
                "key": r.key,
                "group": r.group,
                "status": r.status.value,
                "message": r.message,
                "hint": r.hint,
                "detail": r.detail,
                "error_code": r.error_code,
                "elapsed_ms": r.elapsed_ms,
                "fix_applied": r.fix_applied,
                "data": r.data,
            }
            for r in report.results
        ],
    }
    return json.dumps(doc, ensure_ascii=False, indent=2, default=str)


# ----------------------------------------------------------------------------- registry
def default_checks() -> list[Check]:
    """The documented order (docs/07 §5). Groups added by later units expose ``CHECKS`` in their module."""
    from vaishnavi.bootstrap.checks import autostart, backup, config, database, filesystem, ledger, network, packages, python_env, secrets  # noqa: PLC0415

    checks: list[Check] = [
        python_env.PythonEnvCheck(),
        packages.PackagesCheck(),
        filesystem.FilesystemCheck(),
        config.ConfigCheck(),
        database.DatabaseCheck(),
        ledger.LedgerCheck(),
        backup.BackupCheck(),
        secrets.SecretsCheck(),
        network.NetworkCheck(),
        autostart.AutostartCheck(),
    ]
    for optional in ("system_components", "code_integrity", "ai_provider", "hardware"):
        try:
            module = __import__(f"vaishnavi.bootstrap.checks.{optional}", fromlist=["CHECKS"])
        except ImportError:
            continue
        checks.extend(getattr(module, "CHECKS", ()))
    return checks


def select_checks(checks: Sequence[Check], keys: Sequence[str] | None) -> list[Check]:
    """Subset by key (``--checks a,b``); unknown keys are ignored with a log line."""
    if not keys:
        return list(checks)
    wanted = [k.strip() for k in keys if k.strip()]
    by_key = {c.key: c for c in checks}
    for k in wanted:
        if k not in by_key:
            log.warning("doctor: unknown check %s ignored", k)
    return [by_key[k] for k in wanted if k in by_key]
