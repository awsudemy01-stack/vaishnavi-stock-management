"""``/onboarding`` — the first Owner login (docs/09 §2a, step 1 of the first-day wizard).

Phase 1 ships the one step nothing else can wait for: replace the seeded ``!SETUP`` sentinel with a real
**Owner password + Owner PIN**, then show the **12-word recovery key once** and make the Owner tick
"I have written it down". The remaining wizard steps (shop profile, tax, payments, staff, products,
labels, opening stock, scanner, printer, backup drive) arrive with their own units and reuse this page.
View-model first; NiceGUI at the bottom.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from vaishnavi.app_services import AppServices
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.money import rate_to_bp
from vaishnavi.core.permissions import Actor, PermissionDenied, Role
from vaishnavi.core.security import password_problem, pin_problem
from vaishnavi.db.records import UserRecord
from vaishnavi.services.onboarding import STEPS
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.onboarding")

GSTIN_RE: Final = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


@dataclass(frozen=True)
class OwnerSetupResult:
    """What the page shows after the sentinel is gone."""

    user: UserRecord
    recovery_words: list[str]
    sheet_lines: list[str]


def validate_owner_setup(*, password: str, password_again: str, pin: str, pin_again: str) -> dict[str, str]:
    """Field → plain reason; empty when the form may be submitted."""
    problems: dict[str, str] = {}
    if (p := password_problem(password)) is not None:
        problems["password"] = p
    elif password != password_again:
        problems["password_again"] = "the two passwords differ"
    if (p := pin_problem(pin)) is not None:
        problems["pin"] = p
    elif pin != pin_again:
        problems["pin_again"] = "the two PINs differ"
    return problems


def complete_owner_setup(services: AppServices, *, password: str, pin: str, display_name: str | None) -> OwnerSetupResult:
    """Set password + PIN (``E-AUTH-010`` when already done), issue the recovery key, return the sheet."""
    user = services.users.complete_owner_setup(password, pin, display_name=display_name)
    issued = services.recovery.issue(None)
    sheet = issued.sheet(services.settings.shop.name).splitlines()
    log.info("ONBOARDING_OWNER_SETUP_DONE user=%s key_id=%s", user.id, issued.key_id)
    return OwnerSetupResult(user=user, recovery_words=issued.phrase.split(), sheet_lines=sheet)


def acknowledge_recovery_key(services: AppServices, user: UserRecord) -> None:
    """The Owner ticked "I have written it down"."""
    services.recovery.acknowledge(Actor(user_id=user.id, role=Role.OWNER))


# ============================================================================ wizard steps 2–11
@dataclass(frozen=True)
class StepResult:
    """Outcome of a step submit."""

    ok: bool
    message: str = ""
    error_code: str | None = None
    problems: dict[str, str] = field(default_factory=dict)


def _guard(fn: Any, *args: Any, **kwargs: Any) -> StepResult:
    try:
        fn(*args, **kwargs)
    except PermissionDenied as exc:
        return StepResult(False, str(exc), exc.code)
    except VaishnaviError as exc:
        return StepResult(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    except (ValueError, InvalidOperation) as exc:
        return StepResult(False, str(exc))
    return StepResult(True)


def submit_shop_profile(services: AppServices, actor: Actor, form: dict[str, Any]) -> StepResult:
    """Name (EN/HI), address, phone, GSTIN, state code → settings."""
    problems: dict[str, str] = {}
    if not str(form.get("name") or "").strip():
        problems["name"] = "required"
    if not str(form.get("address_line1") or "").strip():
        problems["address_line1"] = "required"
    gstin = str(form.get("gstin") or "").strip().upper()
    if gstin and not GSTIN_RE.match(gstin):
        problems["gstin"] = "15 characters, e.g. 09ABCDE1234F1Z5"
    if problems:
        return StepResult(False, problems=problems)
    changes = {f"shop.{k}": str(form.get(k) or "").strip() for k in ("name", "name_hi", "address_line1", "address_line2", "phone", "email", "state_code")}
    changes["shop.gstin"] = gstin
    return _guard(services.settings_service.set_many, actor, changes)


def submit_tax(services: AppServices, actor: Actor, form: dict[str, Any]) -> StepResult:
    """Registered? GSTIN, default GST %, prices inclusive? — GSTIN is required (and validated) when registered."""
    try:
        rate_bp = rate_to_bp(str(form.get("default_rate") or "0").rstrip("%"))
    except Exception:  # noqa: BLE001
        return StepResult(False, problems={"default_rate": "not a percentage"})
    registered = bool(form.get("registered"))
    gstin = str(form.get("gstin") or "").strip().upper()
    if gstin and not GSTIN_RE.match(gstin):
        return StepResult(False, problems={"gstin": "15 characters, e.g. 09ABCDE1234F1Z5"})
    if registered and not gstin and not services.settings_service.effective().shop.gstin.strip():
        return StepResult(False, problems={"gstin": "required when GST registered"}, error_code="E-CFG-003")
    changes: dict[str, Any] = {"tax.registered": registered, "tax.default_rate_bp": rate_bp, "tax.prices_include_tax": bool(form.get("prices_include_tax", True))}
    if gstin:
        changes["shop.gstin"] = gstin
    return _guard(services.settings_service.set_many, actor, changes)


def submit_payments(services: AppServices, actor: Actor, form: dict[str, Any]) -> StepResult:
    """UPI id, payee, bank — stored encrypted (never visible to staff or the AI)."""
    upi = str(form.get("upi_id") or "").strip()
    if upi and "@" not in upi:
        return StepResult(False, problems={"upi_id": "a UPI id looks like name@bank"})

    def store() -> None:
        for key in ("upi_id", "payee_name", "bank_name", "account_no", "ifsc"):
            value = str(form.get(key) or "").strip()
            if value:
                services.secure_settings.set(actor, f"payments.{key}", value)
        if upi:
            services.settings_service.set_many(actor, {"receipt.show_qr_upi": True})

    return _guard(store)


def submit_staff(services: AppServices, actor: Actor, form: dict[str, Any]) -> StepResult:
    """One billing-staff account (numeric password allowed)."""
    return _guard(services.users.create_user, actor, str(form.get("username") or "").strip(), str(form.get("display_name") or "").strip(), str(form.get("password") or ""))


@dataclass(frozen=True)
class OpeningRow:
    """A product that has not moved yet."""

    product_id: int
    sku: str
    name: str
    unit: str


@dataclass(frozen=True)
class OpeningStockModel:
    """Table for the opening-stock step."""

    rows: list[OpeningRow]


def build_opening_stock(services: AppServices, texts: UiTexts) -> OpeningStockModel:
    """Products without any movement yet."""
    page = services.catalog.list_products(limit=100000)
    moved = {m.product_id for m in services.inventory.movements(limit=100000)}
    return OpeningStockModel([OpeningRow(p.id, p.sku, p.name, p.base_unit) for p in page.items if p.id not in moved])


def submit_opening_stock(services: AppServices, actor: Actor, quantities: dict[str, str]) -> StepResult:
    """``{product_id: qty}``; blanks and zeros are skipped; unknown ids ignored."""
    def apply() -> None:
        for pid_text, qty_text in quantities.items():
            if not str(qty_text).strip():
                continue
            qty = Decimal(str(qty_text).strip())
            if qty <= 0:
                continue
            pid = int(pid_text)
            if services.catalog.get(pid) is None:
                log.debug("OPENING_STOCK_UNKNOWN_PRODUCT id=%s", pid)
                continue
            services.inventory.opening_stock(actor, pid, qty)

    return _guard(apply)


@dataclass(frozen=True)
class StepView:
    """One rail entry."""

    key: str
    title: str
    done: bool
    skipped: bool
    optional: bool
    detail: str


@dataclass(frozen=True)
class WizardModel:
    """The wizard frame."""

    steps: list[StepView]
    current: StepView
    done_count: int
    progress_text: str
    prev_key: str | None
    next_key: str | None
    labels: dict[str, str] = field(default_factory=dict)


def build_wizard(services: AppServices, texts: UiTexts, *, current: str | None = None) -> WizardModel:
    """Rail + current step (defaults to the first open one)."""
    status = services.onboarding.status()
    views = [StepView(s.key, texts.t(f"setup.step.{s.key}"), s.done, s.skipped, s.optional, s.detail) for s in status]
    key = current if current in STEPS else (services.onboarding.next_step() or STEPS[0])
    idx = STEPS.index(key)
    done = sum(1 for s in status if s.done or s.skipped)
    labels = {k: texts.t(f"setup.{k}") for k in ("title", "next", "back", "skip", "finish", "done", "optional", "skipped")}
    return WizardModel(views, views[idx], done, f"{done}/{len(STEPS)}", STEPS[idx - 1] if idx > 0 else None, STEPS[idx + 1] if idx + 1 < len(STEPS) else None, labels)


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def render_wizard(state: Any, client: Any, texts: UiTexts, *, current: str | None = None) -> None:
    """Rail on the left, the current step's form on the right; every step has Skip/Next; Finish on the last one."""
    from nicegui import ui  # noqa: PLC0415

    services: AppServices = state.extra["services"]
    actor = client.actor()
    box = ui.row().classes("w-full gap-4 no-wrap")

    def goto(key: str | None) -> None:
        ui.navigate.to("/onboarding" if key is None else f"/onboarding?step={key}")

    def report(result: StepResult, next_key: str | None) -> None:
        if result.ok:
            ui.notify(texts.t("common.saved"), type="positive")
            goto(next_key)
            return
        detail = result.message or "; ".join(f"{k}: {v}" for k, v in result.problems.items())
        log.debug("SETUP_STEP_REJECTED code=%s detail=%s", result.error_code, detail)
        ui.notify(detail, type="negative")

    def render() -> None:
        m = build_wizard(services, texts, current=current)
        box.clear()
        with box:
            with ui.column().classes("gap-1").style("width: 260px"):
                ui.label(f"{m.labels['title']} · {m.progress_text}").classes("font-semibold")
                for s in m.steps:
                    icon = "check_circle" if s.done else ("remove_circle_outline" if s.skipped else "radio_button_unchecked")
                    colour = "text-green-700" if s.done else ("text-gray-400" if s.skipped else "text-gray-700")
                    active = " font-bold" if s.key == m.current.key else ""
                    with ui.row().classes(f"items-center gap-2 cursor-pointer {colour}{active}").on("click", lambda _e, k=s.key: goto(k)):
                        ui.icon(icon)
                        ui.label(s.title + (f" ({m.labels['optional']})" if s.optional else ""))
            with ui.column().classes("vsm-card p-6 gap-3 flex-1"):
                ui.label(m.current.title).classes("text-xl font-bold")
                ui.label(texts.t(f"setup.hint.{m.current.key}")).classes("text-sm text-gray-600")
                if m.current.detail:
                    ui.label(m.current.detail).classes("text-sm vsm-mono")
                submit = _STEP_FORMS.get(m.current.key, _render_link_step)(ui, services, actor, texts, m.current.key)
                with ui.row().classes("gap-2 w-full justify-between pt-2"):
                    ui.button(m.labels["back"], on_click=lambda: goto(m.prev_key)).props("flat").set_enabled(m.prev_key is not None)
                    with ui.row().classes("gap-2"):
                        if not m.current.done:
                            ui.button(m.labels["skip"], on_click=lambda: (services.onboarding.skip(actor, m.current.key), goto(m.next_key))).props("flat")
                        if submit is not None:
                            ui.button(m.labels["next"], on_click=lambda: report(submit(), m.next_key)).props("unelevated color=primary")
                        elif m.next_key is not None:
                            ui.button(m.labels["next"], on_click=lambda: goto(m.next_key)).props("unelevated color=primary")
                        if m.next_key is None:
                            ui.button(m.labels["finish"], icon="done_all", on_click=lambda: ui.navigate.to("/")).props("unelevated color=positive")

    render()


def _render_shop_profile(ui: Any, services: AppServices, actor: Actor, texts: UiTexts, key: str) -> Any:
    shop = services.settings_service.effective().shop
    form = {k: getattr(shop, k, "") or "" for k in ("name", "name_hi", "address_line1", "address_line2", "phone", "email", "gstin", "state_code")}
    for k in ("name", "name_hi", "address_line1", "address_line2", "phone", "email", "gstin", "state_code"):
        ui.input(texts.t(f"settings.shop.{k}") if texts.has(f"settings.shop.{k}") else k.replace("_", " ")).bind_value(form, k).classes("w-full")
    return lambda: submit_shop_profile(services, actor, form)


def _render_tax(ui: Any, services: AppServices, actor: Actor, texts: UiTexts, key: str) -> Any:
    eff = services.settings_service.effective()
    tax = eff.tax
    form = {"registered": bool(tax.registered), "gstin": eff.shop.gstin or "", "default_rate": str(Decimal(tax.default_rate_bp) / 100), "prices_include_tax": bool(tax.prices_include_tax)}
    ui.switch("GST registered").bind_value(form, "registered")
    ui.input("GSTIN (needed when registered)", placeholder="09ABCDE1234F1Z5").bind_value(form, "gstin").classes("w-72").props("maxlength=15")
    ui.input("Default GST %", placeholder="5").bind_value(form, "default_rate").classes("w-48").props("inputmode=decimal")
    ui.switch("Shelf prices include tax").bind_value(form, "prices_include_tax")
    return lambda: submit_tax(services, actor, form)


def _render_payments(ui: Any, services: AppServices, actor: Actor, texts: UiTexts, key: str) -> Any:
    form = {k: "" for k in ("upi_id", "payee_name", "bank_name", "account_no", "ifsc")}
    for k, label in (("upi_id", "UPI id (name@bank)"), ("payee_name", "Payee name"), ("bank_name", "Bank"), ("account_no", "Account number"), ("ifsc", "IFSC")):
        ui.input(label).bind_value(form, k).classes("w-full")
    return lambda: submit_payments(services, actor, form)


def _render_staff(ui: Any, services: AppServices, actor: Actor, texts: UiTexts, key: str) -> Any:
    form = {"username": "", "display_name": "", "password": ""}
    ui.input("Login name").bind_value(form, "username").classes("w-full")
    ui.input("Display name").bind_value(form, "display_name").classes("w-full")
    ui.input("Password (6+ characters)", password=True, password_toggle_button=True).bind_value(form, "password").classes("w-full")
    return lambda: submit_staff(services, actor, form)


def _render_opening_stock(ui: Any, services: AppServices, actor: Actor, texts: UiTexts, key: str) -> Any:
    model = build_opening_stock(services, texts)
    quantities: dict[str, str] = {}
    if not model.rows:
        ui.label(texts.t("setup.step.products") + " → 0").classes("text-sm text-gray-500")
        return None
    with ui.scroll_area().classes("w-full").style("max-height: 50vh"):
        with ui.grid(columns=3).classes("w-full gap-1 items-center"):
            for row in model.rows:
                ui.label(row.sku).classes("vsm-mono text-sm")
                ui.label(row.name)
                ui.input(row.unit).bind_value(quantities, str(row.product_id)).props("dense inputmode=decimal").classes("w-28")
    return lambda: submit_opening_stock(services, actor, quantities)


_STEP_LINKS: Final = {"products": "/products", "labels": "/labels", "scanner": "/settings?tab=hardware", "printer": "/settings?tab=hardware", "backup_drive": "/settings?tab=backups", "recovery_key": "/settings?tab=system"}


def _render_link_step(ui: Any, services: AppServices, actor: Actor, texts: UiTexts, key: str) -> Any:
    """Steps that are done on their own page: a button to open it; the probe marks the step done."""
    target = _STEP_LINKS.get(key, "/")
    ui.button(texts.t(f"setup.step.{key}"), icon="open_in_new", on_click=lambda: ui.navigate.to(target)).props("outline color=primary")
    return None


_STEP_FORMS: Final = {"shop_profile": _render_shop_profile, "tax": _render_tax, "payments": _render_payments, "staff": _render_staff, "opening_stock": _render_opening_stock}


def register(state: Any) -> None:
    """``/onboarding`` — reachable without a session while the Owner setup is pending."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.ui import guards, layout  # noqa: PLC0415
    from vaishnavi.ui.pages.login import sign_in  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client, save_client  # noqa: PLC0415

    services: AppServices = state.extra["services"]

    @ui.page("/onboarding")
    def _onboarding(step: str | None = None) -> None:
        client = load_client(app)
        texts = UiTexts.load(client.lang)
        if not services.users.owner_setup_pending():
            decision = guards.decide("/onboarding", client)
            if decision is not guards.Decision.ALLOW:
                ui.navigate.to(guards.redirect_for(decision, "/onboarding"))
                return
            with frame(state, current_path="/onboarding"):
                render_wizard(state, client, texts, current=step)
            return
        layout.render_head()
        form = {"name": "", "password": "", "password_again": "", "pin": "", "pin_again": ""}
        with ui.column().classes("items-center justify-center w-full min-h-screen p-6"):
            card = ui.column().classes("vsm-card p-6 gap-2").style("width: 560px")

            def render_form() -> None:
                card.clear()
                with card:
                    ui.label(texts.t("onboarding.title")).classes("text-xl font-bold")
                    ui.label(texts.t("onboarding.intro")).classes("text-sm text-gray-600")
                    ui.input(texts.t("onboarding.name")).bind_value(form, "name").classes("w-full")
                    ui.input(texts.t("onboarding.password"), password=True, password_toggle_button=True).bind_value(form, "password").classes("w-full")
                    ui.input(texts.t("onboarding.password_again"), password=True).bind_value(form, "password_again").classes("w-full")
                    ui.input(texts.t("onboarding.pin"), password=True).bind_value(form, "pin").classes("w-full").props("inputmode=numeric")
                    ui.input(texts.t("onboarding.pin_again"), password=True).bind_value(form, "pin_again").classes("w-full").props("inputmode=numeric")
                    ui.button(texts.t("common.confirm"), on_click=submit).props("unelevated color=primary").classes("w-full vsm-touch")

            def submit() -> None:
                problems = validate_owner_setup(password=form["password"], password_again=form["password_again"], pin=form["pin"], pin_again=form["pin_again"])
                if problems:
                    ui.notify("; ".join(f"{texts.t('onboarding.' + k)}: {v}" for k, v in problems.items()), type="negative")
                    return
                try:
                    result = complete_owner_setup(services, password=form["password"], pin=form["pin"], display_name=form["name"] or None)
                except VaishnaviError as exc:
                    ui.notify(f"{exc.user_message(client.lang)} {exc.what_to_do(client.lang)}", type="negative")
                    return
                outcome = sign_in(services.users, services.sessions, client, username=result.user.username, password=form["password"], client_label="onboarding")
                form["password"] = form["password_again"] = form["pin"] = form["pin_again"] = ""
                save_client(app, client)
                render_sheet(result, outcome.next_path or "/")

            def render_sheet(result: OwnerSetupResult, next_path: str) -> None:
                card.clear()
                ticked = {"ok": False}
                with card:
                    ui.label(texts.t("onboarding.key_title")).classes("text-xl font-bold")
                    ui.label(texts.t("onboarding.key_intro")).classes("text-sm text-gray-600")
                    with ui.grid(columns=4).classes("w-full gap-1 vsm-mono"):
                        for i, word in enumerate(result.recovery_words, 1):
                            ui.label(f"{i:2d}. {word}").classes("p-1 rounded").style("background:#FFF3E0")
                    ui.button(texts.t("onboarding.key_print"), icon="print", on_click=lambda: ui.run_javascript("window.print()")).props("flat")
                    ui.checkbox(texts.t("onboarding.key_ack")).bind_value(ticked, "ok")

                    def done() -> None:
                        if not ticked["ok"]:
                            ui.notify(texts.t("onboarding.key_must_ack"), type="warning")
                            return
                        acknowledge_recovery_key(services, result.user)
                        ui.navigate.to(next_path)

                    ui.button(texts.t("onboarding.key_done"), on_click=done).props("unelevated color=primary").classes("w-full vsm-touch")

            render_form()


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
