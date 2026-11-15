"""HardwareGuard — *no scan → no sale*, printer optional, Owner overrides time-boxed (docs/05 §10; stdlib).

The guard owns the live **hardware state** (paired scanner present? configured printer ready? any new
printer appeared?) refreshed by the 10-second ``hardware_poll`` job, and answers two questions for the
POS:

* :meth:`can_add_line` — a cashier may add a line only by **scanning** while ``billing.require_scan``
  is on (typed codes → ``E-BILL-070``), and only while the paired scanner is present (``E-SCN-001``).
  The Owner is never blocked. A probe *failure* leaves presence ``None`` (unknown) and does not lock
  the counter — only a confirmed absence does.
* :meth:`can_checkout` — with ``billing.require_printer`` on, an unready printer blocks the sale
  (``E-PRN-002``) unless an Owner **override** is active; with it off (default) the sale goes through
  and the receipt is queued.

Overrides live in ``hardware_override`` with an expiry, need the Owner (or a PIN-verified cashier with
the Owner's id as approver), are audited as ``HARDWARE_OVERRIDE`` and appear in the overrides report.
Every state change publishes ``HardwareStatusChanged`` (scanner lost → WARN notification; new printer →
"set it as the receipt printer?" toast).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Protocol

from vaishnavi.core.clock import iso_utc, now_utc
from vaishnavi.core.errors import BillingError, PrinterError, ScannerError
from vaishnavi.core.events import EventBus, HardwareStatusChanged, bus
from vaishnavi.core.permissions import Actor, Permission, Role, require
from vaishnavi.core.settings import HardwareSettings, Settings
from vaishnavi.db.records import OverrideRecord
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.hardware_guard")

OVERRIDE_KINDS: Final = ("PRINTER", "SCANNER")
MAX_OVERRIDE_MINUTES: Final = 12 * 60


@dataclass(frozen=True)
class HardwareState:
    """What the last poll saw."""

    checked_at: str
    scanner_paired: bool
    scanner_present: bool | None
    """None = could not probe (treated as present so a driver hiccup never stops the shop)."""
    printer_configured: bool
    printer_ready: bool
    scanner_detail: str = ""
    printer_detail: str = ""
    new_printers: tuple[str, ...] = ()

    @property
    def scanner_ok(self) -> bool:
        """Paired and not confirmed absent."""
        return self.scanner_paired and self.scanner_present is not False


class OverrideStore(Protocol):
    """Rows the guard keeps."""

    def insert_override(self, session: Session, *, kind: str, owner_user_id: int, started_at: str, expires_at: str, reason: str | None) -> int: ...  # pragma: no cover - protocol
    def active_override(self, session: Session, *, kind: str, now: str) -> OverrideRecord | None: ...  # pragma: no cover - protocol
    def expire_override(self, session: Session, override_id: int, *, at: str) -> None: ...  # pragma: no cover - protocol


class StatusLike(Protocol):
    """``platform.printing.PrinterStatus`` shape."""

    kind: str
    address: str
    ready: bool
    detail: str


class HardwareGuard:
    """Strict-mode gatekeeper + hot-plug watcher."""

    def __init__(
        self,
        txn: TxnFactory,
        store: OverrideStore,
        audit: AuditService,
        *,
        settings: Callable[[], Settings],
        scanner_present: Callable[[HardwareSettings], bool | None],
        printer_status: Callable[[HardwareSettings], StatusLike],
        discover_printers: Callable[[], list[str]] | None = None,
        event_bus: EventBus = bus,
    ) -> None:
        self._txn = txn
        self._store = store
        self._audit = audit
        self._settings = settings
        self._scanner_present = scanner_present
        self._printer_status = printer_status
        self._discover = discover_printers or (lambda: [])
        self._bus = event_bus
        self._state: HardwareState | None = None
        self._known_printers: set[str] | None = None

    # -- state ------------------------------------------------------------------------------------
    def state(self) -> HardwareState:
        """Last poll (probing once when nothing was polled yet)."""
        return self._state if self._state is not None else self.refresh()

    def poll(self) -> HardwareState:
        """Scheduler entry — never raises."""
        try:
            return self.refresh()
        except Exception as exc:  # noqa: BLE001
            log.error("HARDWARE_POLL_FAILED %r", exc, exc_info=True)
            return self._state or HardwareState(iso_utc(), False, None, False, False, str(exc), str(exc))

    def refresh(self) -> HardwareState:
        """Probe scanner + printer (+ discovery), publish changes, cache."""
        hw = self._settings().hardware
        paired = bool(hw.scanner_vid_pid)
        present: bool | None = None
        scanner_detail = "no scanner paired" if not paired else ""
        if paired:
            try:
                present = self._scanner_present(hw)
                scanner_detail = "connected" if present else ("NOT connected" if present is False else "unknown")
            except Exception as exc:  # noqa: BLE001
                present = None
                scanner_detail = f"probe failed: {exc}"
                log.warning("SCANNER_PROBE_FAILED %s", exc)
        try:
            st = self._printer_status(hw)
            printer_ready, printer_detail = bool(st.ready), st.detail
        except Exception as exc:  # noqa: BLE001
            printer_ready, printer_detail = False, f"probe failed: {exc}"
            log.warning("PRINTER_PROBE_FAILED %s", exc)
        try:
            found = set(self._discover())
        except Exception as exc:  # noqa: BLE001
            found = set()
            log.debug("printer discovery failed: %s", exc)
        new_printers: tuple[str, ...] = ()
        if self._known_printers is None:
            self._known_printers = found
        else:
            fresh = sorted(found - self._known_printers)
            self._known_printers |= found
            new_printers = tuple(fresh)
        state = HardwareState(iso_utc(), paired, present, hw.printer_kind != "none", printer_ready, scanner_detail, printer_detail, new_printers)
        self._publish_changes(self._state, state)
        self._state = state
        log.debug("HARDWARE_STATE scanner=%s/%s printer=%s/%s new=%s", paired, present, state.printer_configured, printer_ready, new_printers)
        return state

    # -- rules ------------------------------------------------------------------------------------
    def can_add_line(self, actor: Actor, *, via_scan: bool) -> None:
        """Raise when a cashier is about to add a line the strict mode forbids."""
        if actor.role is Role.OWNER or not self._settings().billing.require_scan:
            return
        if self.active_override("SCANNER") is not None:
            return
        if not via_scan:
            log.info("TYPED_CODE_REJECTED user=%s code=E-BILL-070", actor.user_id)
            raise BillingError("E-BILL-070", detail="typed input into the scan box")
        st = self.state()
        if not st.scanner_ok:
            log.warning("SCANNER_REQUIRED user=%s paired=%s present=%s", actor.user_id, st.scanner_paired, st.scanner_present)
            raise ScannerError("E-SCN-001", detail=st.scanner_detail)

    def can_checkout(self, actor: Actor, cart: Any) -> None:
        """BillingService pre-checkout hook."""
        s = self._settings()
        st = self.state()
        if s.billing.require_scan and actor.role is not Role.OWNER and not st.scanner_ok and self.active_override("SCANNER") is None:
            raise ScannerError("E-SCN-001", detail=st.scanner_detail)
        if s.billing.require_printer and not st.printer_ready and self.active_override("PRINTER") is None:
            log.warning("PRINTER_REQUIRED_BLOCK user=%s detail=%s code=E-PRN-002", actor.user_id, st.printer_detail)
            raise PrinterError("E-PRN-002", detail=st.printer_detail)

    def may_enable_require_printer(self) -> bool:
        """Settings guard: strictness can only be switched on while a printer is configured and ready."""
        st = self.refresh()
        return st.printer_configured and st.printer_ready

    def scan_mode(self, actor: Actor) -> str:
        """``SCANNER`` (typing rejected) or ``TYPED_ALLOWED`` — for the POS scan box."""
        if actor.role is Role.OWNER or not self._settings().billing.require_scan or self.active_override("SCANNER") is not None:
            return "TYPED_ALLOWED"
        return "SCANNER"

    # -- overrides --------------------------------------------------------------------------------
    def start_override(self, actor: Actor, kind: str, *, minutes: int, reason: str, approver_id: int | None = None) -> OverrideRecord:
        """Owner (or PIN) allows selling without the device for a while; audited."""
        if kind not in OVERRIDE_KINDS:
            raise ValueError(f"kind must be one of {OVERRIDE_KINDS}")
        if minutes <= 0 or minutes > MAX_OVERRIDE_MINUTES:
            raise ValueError(f"minutes must be 1..{MAX_OVERRIDE_MINUTES}")
        if not reason or not reason.strip():
            raise ValueError("a reason is required")
        require(actor, Permission.DISCOUNT_OVER_CAP)  # the generic "Owner PIN" grant: ALLOW for Owner, PIN for staff
        owner_id = actor.user_id if actor.role is Role.OWNER else approver_id
        if owner_id is None:
            raise ValueError("approver_id (the Owner) is required when a cashier enters the PIN")
        now = now_utc()
        started, expires = iso_utc(now), iso_utc(now + timedelta(minutes=minutes))
        with self._txn(label="hw.override") as session:
            oid = self._store.insert_override(session, kind=kind, owner_user_id=owner_id, started_at=started, expires_at=expires, reason=reason.strip())
            self._audit.record(session, "HARDWARE_OVERRIDE", user_id=actor.user_id, actor_user_id=owner_id, entity="hardware_override", entity_id=oid, after={"kind": kind, "minutes": minutes, "reason": reason.strip(), "expires_at": expires})
        log.warning("HARDWARE_OVERRIDE kind=%s minutes=%d by=%s owner=%s reason=%r", kind, minutes, actor.user_id, owner_id, reason)
        return OverrideRecord(oid, kind, owner_id, started, expires, reason.strip())

    def active_override(self, kind: str) -> OverrideRecord | None:
        """The unexpired override for ``kind`` (None when selling must follow the rules)."""
        with self._txn(label="hw.override_get") as session:
            return self._store.active_override(session, kind=kind, now=iso_utc())

    def end_override(self, actor: Actor, kind: str) -> None:
        """Owner ends an override early."""
        require(actor, Permission.MANAGE_SYSTEM)
        rec = self.active_override(kind)
        if rec is None:
            return
        with self._txn(label="hw.override_end") as session:
            self._store.expire_override(session, rec.id, at=iso_utc())
            self._audit.record(session, "HARDWARE_OVERRIDE_ENDED", user_id=actor.user_id, entity="hardware_override", entity_id=rec.id)
        log.info("HARDWARE_OVERRIDE_ENDED kind=%s id=%s by=%s", kind, rec.id, actor.user_id)

    # -- internals --------------------------------------------------------------------------------
    def _publish_changes(self, before: HardwareState | None, after: HardwareState) -> None:
        if before is not None:
            if before.scanner_paired and after.scanner_paired and before.scanner_present is not None and after.scanner_present is not None and before.scanner_present != after.scanner_present:
                self._bus.publish(HardwareStatusChanged(device="scanner", present=bool(after.scanner_present), detail=after.scanner_detail))
                log.info("SCANNER_%s detail=%s", "PRESENT" if after.scanner_present else "ABSENT", after.scanner_detail)
            if before.printer_configured and after.printer_configured and before.printer_ready != after.printer_ready:
                self._bus.publish(HardwareStatusChanged(device="receipt_printer", present=after.printer_ready, detail=after.printer_detail))
                log.info("PRINTER_%s detail=%s", "READY" if after.printer_ready else "NOT_READY", after.printer_detail)
        for name in after.new_printers:
            self._bus.publish(HardwareStatusChanged(device="printer", present=True, detail=name))
            log.info("PRINTER_APPEARED name=%s", name)


# ----------------------------------------------------------------------------- default probes
def scanner_probe(hw: HardwareSettings) -> bool | None:
    """Is the paired USB scanner plugged in? (``platform.devices``; None when the OS gave no list)."""
    from vaishnavi.platform import devices  # noqa: PLC0415

    if not hw.scanner_vid_pid:
        return None
    listing = devices.list_usb_devices()
    if not listing:
        return None
    return devices.find_device(hw.scanner_vid_pid) is not None


def discover_printer_names() -> list[str]:
    """Names of printers the OS knows plus USB printer-class devices (for the "new printer" toast)."""
    from vaishnavi.platform import devices  # noqa: PLC0415
    from vaishnavi.platform.printing import list_system_printers  # noqa: PLC0415

    names = [p.name for p in list_system_printers()]
    for dev in devices.list_usb_devices():
        if "print" in (dev.device_class or "").lower() or "print" in (dev.name or "").lower():
            names.append(f"{dev.name or dev.vid_pid} (USB)")
    return names
