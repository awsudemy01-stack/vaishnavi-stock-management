"""Send a customer their bill on WhatsApp (live run 4 follow-up).

Two honest delivery paths, chosen at send time:

* **Cloud API** — when the Owner has configured Meta's WhatsApp Cloud API under Settings → Publishing
  (phone-number id + access token), the bill text is posted to the customer's number and the send is
  audited. Meta only lets a business *start* a conversation with an approved template, so this path
  works when the customer messaged the shop's number in the last 24 h or the Owner has a template;
  any API failure falls back to the link below instead of failing the sale.
* **Click-to-chat link** — always available: ``https://wa.me/<number>?text=<bill>`` opens WhatsApp
  Desktop / Web on the counter PC with the whole bill pre-filled; the cashier taps *Send*. No automation
  of WhatsApp's UI, no unofficial API, nothing to install.

The message is the same receipt the printer gets (58 mm text, 32 columns, so it fits a phone screen)
inside a WhatsApp monospace block, with the shop name in bold on top.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.core.settings import Settings
from vaishnavi.db.records import SaleView
from vaishnavi.services.audit import AuditService
from vaishnavi.services.billing import BillingService, normalise_phone
from vaishnavi.services.common import TxnFactory
from vaishnavi.services.receipt import ReceiptService

log = logging.getLogger("vaishnavi.services.receipt_share")

WA_API = "https://graph.facebook.com/v19.0"
MAX_WA_TEXT = 4000
"""Cloud API text limit (4096) with headroom."""


@dataclass(frozen=True)
class ShareOutcome:
    """What the POS shows after *Send on WhatsApp*."""

    ok: bool
    via: str  # api | link | none
    link: str | None = None
    phone_text: str = ""
    message: str = ""


class ReceiptShareService:
    """Bill → WhatsApp text → Cloud API or click-to-chat link."""

    def __init__(self, txn: TxnFactory, receipts: ReceiptService, billing: BillingService, audit: AuditService, *, settings: Callable[[], Settings], secret: Callable[[str], str | None], post: Callable[..., dict[str, Any]] | None = None, text_renderer: Callable[[bytes], list[str]]) -> None:
        self._txn, self._receipts, self._billing, self._audit = txn, receipts, billing, audit
        self._settings, self._secret, self._post, self._text_lines = settings, secret, post, text_renderer

    # -- text -------------------------------------------------------------------------------------
    def text_for(self, sale: SaleView) -> str:
        """The WhatsApp message: bold shop name, bill number, the 32-column receipt in a monospace block."""
        shop = self._settings().shop
        model = self._receipts.build(sale)
        body = "\n".join(self._text_lines(self._receipts.escpos(model, paper_mm=58)))
        header = f"*{shop.name}*\nBill {sale.invoice_no}"
        text = f"{header}\n```\n{body}\n```"
        if len(text) > MAX_WA_TEXT:
            text = text[: MAX_WA_TEXT - 6] + "\n```"
        return text

    @staticmethod
    def link_for(phone_digits: str, text: str) -> str:
        """``https://wa.me/919876543210?text=…`` (RFC 3986 percent-encoding; WhatsApp decodes ``%0A`` as new lines)."""
        return f"https://wa.me/{phone_digits}?text={quote(text, safe='')}"

    # -- send -------------------------------------------------------------------------------------
    def enabled(self) -> bool:
        """The Owner's switch (``receipt.whatsapp_share``) — off by default; nothing WhatsApp-related shows when off."""
        return bool(self._settings().receipt.whatsapp_share)

    def api_configured(self) -> bool:
        """True when Settings → Publishing has a WhatsApp phone-number id and a token (and we have an HTTP client)."""
        pub = self._settings().publishing
        return bool(self._post) and bool(pub.whatsapp_phone_id.strip()) and bool(self._secret("publish.whatsapp_token"))

    def share(self, actor: Actor, sale_id: int, *, phone: str | None = None, name: str = "") -> ShareOutcome:
        """Send the bill for ``sale_id`` to ``phone`` (or the sale's customer). Never raises."""
        require(actor, Permission.SELL)
        if not self.enabled():
            return ShareOutcome(False, "none", message="WhatsApp bills are switched off (Settings → Shop & GST)")
        sale = self._receipts.load(sale_id)
        if sale is None:
            return ShareOutcome(False, "none", message="bill not found")
        try:
            digits, phone_text = self._resolve_phone(actor, sale, phone, name)
        except ValueError as exc:
            return ShareOutcome(False, "none", message=str(exc))
        if not digits:
            return ShareOutcome(False, "none", message="no mobile number for this bill")
        text = self.text_for(sale)
        link = self.link_for(digits, text)
        if self.api_configured():
            try:
                self._send_api(digits, text)
            except Exception as exc:  # noqa: BLE001 - the link is the fallback, the sale is already done
                log.warning("RECEIPT_WHATSAPP_API_FAILED sale=%s to=…%s error=%s — falling back to click-to-chat", sale.id, digits[-4:], exc)
            else:
                self._audit_share(actor, sale, digits, "api")
                log.info("RECEIPT_WHATSAPP_SENT sale=%s invoice=%s to=…%s via=api", sale.id, sale.invoice_no, digits[-4:])
                return ShareOutcome(True, "api", link, phone_text)
        self._audit_share(actor, sale, digits, "link")
        log.info("RECEIPT_WHATSAPP_LINK sale=%s invoice=%s to=…%s chars=%d", sale.id, sale.invoice_no, digits[-4:], len(text))
        return ShareOutcome(True, "link", link, phone_text)

    # -- internals --------------------------------------------------------------------------------
    def _resolve_phone(self, actor: Actor, sale: SaleView, phone: str | None, name: str) -> tuple[str | None, str]:
        if phone and phone.strip():
            record = self._billing.remember_customer(actor, phone=phone, name=name)
            return record.phone, record.phone_text
        record = self._billing.customer_of(sale)
        if record is None or not record.phone:
            return None, ""
        return normalise_phone(record.phone), record.phone_text

    def _send_api(self, digits: str, text: str) -> None:
        assert self._post is not None
        pub = self._settings().publishing
        token = self._secret("publish.whatsapp_token") or ""
        self._post(f"{WA_API}/{pub.whatsapp_phone_id.strip()}/messages", headers={"Authorization": f"Bearer {token}"}, json_body={"messaging_product": "whatsapp", "to": digits, "type": "text", "text": {"body": text}})

    def _audit_share(self, actor: Actor, sale: SaleView, digits: str, via: str) -> None:
        with self._txn(label="receipt.share") as session:
            self._audit.record(session, "RECEIPT_SHARED", user_id=actor.user_id, entity="sale", entity_id=sale.id, after={"invoice_no": sale.invoice_no, "via": via, "to": f"…{digits[-4:]}"})
