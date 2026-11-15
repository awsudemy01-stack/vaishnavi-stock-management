"""Doctor check: paired scanner present (strict mode), printer ready (never a blocker), scale port (stdlib).

Severity follows docs/07 §5: scanner missing is **WARN** at start-up so the Owner can log in and fix
it, while the POS stays paused for staff (``billing_paused_for_staff``); the printer is INFO/WARN only;
the scale is checked only when configured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext
from vaishnavi.platform import devices, printing, serial_ports

log = logging.getLogger("vaishnavi.bootstrap.checks.hardware")


@dataclass
class HardwareCheck:
    """One line that says scanner · printer · scale state; worst of the three decides."""

    key: str = "hardware"
    group: str = "hardware"

    def run(self, ctx: DoctorContext) -> CheckResult:
        """Probe the three devices."""
        hw = ctx.settings.hardware
        strict = ctx.settings.billing.require_scan
        notes: list[str] = []
        code = ""
        hint = ""
        warn = False

        # Scanner (mandatory for selling)
        scanner_present = False
        if not hw.scanner_vid_pid:
            notes.append(ctx.t("doctor.hardware.scanner_unpaired"))
            code, hint, warn = "E-SCN-001", ctx.t("doctor.hardware.scanner_unpaired_hint"), True
        else:
            dev = devices.find_device(hw.scanner_vid_pid)
            scanner_present = dev is not None
            if scanner_present:
                notes.append(ctx.t("doctor.hardware.scanner_ok", name=(dev.name if dev and dev.name else hw.scanner_vid_pid)))
            else:
                notes.append(ctx.t("doctor.hardware.scanner_missing", id=hw.scanner_vid_pid))
                code, hint, warn = "E-SCN-001", ctx.t("doctor.hardware.scanner_missing_hint"), True

        # Printer (optional)
        status = printing.printer_status(hw)
        if not status.configured:
            notes.append(ctx.t("doctor.hardware.printer_none"))
        elif status.ready:
            notes.append(ctx.t("doctor.hardware.printer_ok", address=status.address))
        else:
            notes.append(ctx.t("doctor.hardware.printer_down", detail=status.detail))
            if not warn:
                code, hint = "E-PRN-001", ctx.t("doctor.hardware.printer_down_hint")
            warn = True

        # Scale (only when configured)
        scale_present: bool | None = None
        if hw.scale_port:
            scale_present = serial_ports.port_present(hw.scale_port)
            if scale_present:
                notes.append(ctx.t("doctor.hardware.scale_ok", port=hw.scale_port))
            else:
                notes.append(ctx.t("doctor.hardware.scale_missing", port=hw.scale_port))
                if not warn:
                    code, hint = "E-SCALE-001", ctx.t("doctor.hardware.scale_missing_hint")
                warn = True

        data = {
            "scanner_paired": bool(hw.scanner_vid_pid),
            "scanner_present": scanner_present,
            "billing_paused_for_staff": strict and not scanner_present,
            "printer": {"kind": status.kind, "address": status.address, "ready": status.ready, "detail": status.detail},
            "scale_port": hw.scale_port,
            "scale_present": scale_present,
        }
        message = " · ".join(notes)
        if warn:
            return CheckResult.warn(message, error_code=code, hint=hint, **data)
        return CheckResult.ok(message, **data)


CHECKS = [HardwareCheck()]
