"""Roles and the permission matrix of docs/05 §5 as code (stdlib only).

Exactly two roles — ``OWNER`` and ``STAFF``. Every capability is a :class:`Permission`; each role
maps a permission to a :class:`Grant`: ``ALLOW``, ``PIN`` (allowed only after the Owner PIN is
entered on the same screen; audited with both user ids), ``POLICY`` (depends on a runtime setting,
e.g. ``manual_add_policy``) or ``DENY``. Services enforce this with ``check()`` / ``require()``;
the UI only *reflects* it. Changing a grant means changing this table and the document together.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class Role(str, Enum):
    """The two roles that exist (ADR-011)."""

    OWNER = "OWNER"
    STAFF = "STAFF"


class Grant(str, Enum):
    """How a role may exercise a permission."""

    ALLOW = "ALLOW"
    PIN = "PIN"
    POLICY = "POLICY"
    DENY = "DENY"


class Permission(str, Enum):
    """Capabilities, grouped as in the matrix."""

    # Selling
    SELL = "SELL"  # bill / scan / F3 find / pay / print
    OPEN_ITEM = "OPEN_ITEM"  # manual line without barcode (per manual_add_policy)
    DISCOUNT_WITHIN_CAP = "DISCOUNT_WITHIN_CAP"
    DISCOUNT_OVER_CAP = "DISCOUNT_OVER_CAP"  # also price override, remove an offer
    HOLD_RESUME_REPRINT = "HOLD_RESUME_REPRINT"
    VOID_REFUND = "VOID_REFUND"
    SHIFT_OWN = "SHIFT_OWN"  # open/close own shift, cash-in
    CASH_OUT = "CASH_OUT"
    VIEW_STOCK_LEVEL = "VIEW_STOCK_LEVEL"
    # Money & pricing
    EDIT_PRICES = "EDIT_PRICES"  # selling price, MRP, cost, tax rate
    MANAGE_OFFERS = "MANAGE_OFFERS"  # offers, discount values, caps
    VIEW_BANK_DETAILS = "VIEW_BANK_DETAILS"  # masked; re-auth to edit
    # Stock
    MANAGE_PRODUCTS = "MANAGE_PRODUCTS"  # create/edit products, barcodes, labels
    PRINT_LABEL = "PRINT_LABEL"  # replacement label only
    STOCK_MOVEMENTS = "STOCK_MOVEMENTS"  # purchase, adjustment, damage, stock-take approval
    # Reports
    REPORT_OWN_SHIFT = "REPORT_OWN_SHIFT"
    REPORT_SALES = "REPORT_SALES"
    REPORT_PURCHASES = "REPORT_PURCHASES"
    REPORT_PROFIT = "REPORT_PROFIT"
    REPORT_AUDIT = "REPORT_AUDIT"  # shrinkage, overrides, discounts, manual lines, cashier perf, chat security, tax
    REPORT_FORECAST = "REPORT_FORECAST"
    # System
    MANAGE_SYSTEM = "MANAGE_SYSTEM"  # users, settings, hardware, backups, restore, logs, doctor, shutdown
    MANAGE_APPLICATION = "MANAGE_APPLICATION"  # version, migrations, config files, update
    # AI assistant
    AI_STOCK_HELP = "AI_STOCK_HELP"
    AI_BUSINESS = "AI_BUSINESS"  # sales / purchase / profit / forecast / cashier questions
    AI_CONFIGURE = "AI_CONFIGURE"  # instructions, language default, guardrail settings, blocked phrases
    AI_SECURITY_REPORT = "AI_SECURITY_REPORT"


_A, _P, _Y, _D = Grant.ALLOW, Grant.PIN, Grant.POLICY, Grant.DENY

MATRIX: Final[dict[Permission, dict[Role, Grant]]] = {
    Permission.SELL: {Role.OWNER: _A, Role.STAFF: _A},
    Permission.OPEN_ITEM: {Role.OWNER: _A, Role.STAFF: _Y},
    Permission.DISCOUNT_WITHIN_CAP: {Role.OWNER: _A, Role.STAFF: _A},
    Permission.DISCOUNT_OVER_CAP: {Role.OWNER: _A, Role.STAFF: _P},
    Permission.HOLD_RESUME_REPRINT: {Role.OWNER: _A, Role.STAFF: _A},
    Permission.VOID_REFUND: {Role.OWNER: _A, Role.STAFF: _P},
    Permission.SHIFT_OWN: {Role.OWNER: _A, Role.STAFF: _A},
    Permission.CASH_OUT: {Role.OWNER: _A, Role.STAFF: _P},
    Permission.VIEW_STOCK_LEVEL: {Role.OWNER: _A, Role.STAFF: _A},
    Permission.EDIT_PRICES: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.MANAGE_OFFERS: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.VIEW_BANK_DETAILS: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.MANAGE_PRODUCTS: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.PRINT_LABEL: {Role.OWNER: _A, Role.STAFF: _A},
    Permission.STOCK_MOVEMENTS: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.REPORT_OWN_SHIFT: {Role.OWNER: _A, Role.STAFF: _A},
    Permission.REPORT_SALES: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.REPORT_PURCHASES: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.REPORT_PROFIT: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.REPORT_AUDIT: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.REPORT_FORECAST: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.MANAGE_SYSTEM: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.MANAGE_APPLICATION: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.AI_STOCK_HELP: {Role.OWNER: _A, Role.STAFF: _A},
    Permission.AI_BUSINESS: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.AI_CONFIGURE: {Role.OWNER: _A, Role.STAFF: _D},
    Permission.AI_SECURITY_REPORT: {Role.OWNER: _A, Role.STAFF: _D},
}
"""docs/05 §5 — keep the two in step; ``tests/core/test_core_permissions.py`` checks completeness."""


class OpenItemPolicy(str, Enum):
    """Runtime setting ``manual_add_policy`` that resolves the ``POLICY`` grant for ``OPEN_ITEM``."""

    ALLOW = "ALLOW"
    PIN = "PIN"
    BLOCK = "BLOCK"


class PermissionDenied(Exception):
    """Raised by ``require()``; error code E-AUTH-010 (docs/08)."""

    code = "E-AUTH-010"

    def __init__(self, permission: Permission, role: Role, grant: Grant) -> None:
        super().__init__(f"{role.value} may not {permission.value} (grant={grant.value})")
        self.permission = permission
        self.role = role
        self.grant = grant


class PinRequired(Exception):
    """Raised by ``require()`` when the action needs the Owner PIN first; error code E-AUTH-011."""

    code = "E-AUTH-011"

    def __init__(self, permission: Permission, role: Role) -> None:
        super().__init__(f"{role.value} needs the Owner PIN for {permission.value}")
        self.permission = permission
        self.role = role


@dataclass(frozen=True)
class Actor:
    """Who is acting: the logged-in user and whether an Owner PIN override is active for this action."""

    user_id: int
    role: Role
    pin_verified: bool = False
    """True when the Owner PIN was entered on the same screen for this action (services audit it)."""


def grant_for(permission: Permission, role: Role, open_item_policy: OpenItemPolicy = OpenItemPolicy.ALLOW) -> Grant:
    """Effective grant, resolving ``POLICY`` through the runtime setting."""
    grant = MATRIX[permission][role]
    if grant is Grant.POLICY:
        return {OpenItemPolicy.ALLOW: Grant.ALLOW, OpenItemPolicy.PIN: Grant.PIN, OpenItemPolicy.BLOCK: Grant.DENY}[open_item_policy]
    return grant


def check(actor: Actor, permission: Permission, *, open_item_policy: OpenItemPolicy = OpenItemPolicy.ALLOW) -> Grant:
    """Grant that applies to this actor now (``PIN`` becomes ``ALLOW`` when the PIN was verified)."""
    grant = grant_for(permission, actor.role, open_item_policy)
    if grant is Grant.PIN and actor.pin_verified:
        return Grant.ALLOW
    return grant


def require(actor: Actor, permission: Permission, *, open_item_policy: OpenItemPolicy = OpenItemPolicy.ALLOW) -> None:
    """Raise ``PinRequired`` or ``PermissionDenied`` unless the actor may proceed."""
    grant = check(actor, permission, open_item_policy=open_item_policy)
    if grant is Grant.ALLOW:
        return
    if grant is Grant.PIN:
        raise PinRequired(permission, actor.role)
    raise PermissionDenied(permission, actor.role, grant)


def permissions_of(role: Role, open_item_policy: OpenItemPolicy = OpenItemPolicy.ALLOW) -> dict[Permission, Grant]:
    """Full effective map for a role — used by the UI to render what is possible."""
    return {p: grant_for(p, role, open_item_policy) for p in Permission}
