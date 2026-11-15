"""AppServices — builds every service once at start-up (docs/10 §15).

Phase-1 services run on the stdlib sqlite3 stores (``db/sqlite_stores.py``), which keeps the spine free
of the ORM wheel; the UI/billing units add ORM-backed repositories for the same protocols. Secrets use
``FernetBox`` when ``cryptography`` is importable and the master key could be loaded, otherwise
``NullBox`` (which refuses to store anything in clear) — reported as ``E-SEC-001`` by the doctor.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vaishnavi.core.events import EventBus, bus
from vaishnavi.core.security import NullBox, SecretBox
from vaishnavi.core.settings import Settings
from vaishnavi.db.guardian import DbGuardian
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteBillingStore, SqliteCatalogStore, SqliteChatStore, SqliteForecastStore, SqliteHardwareStore, SqliteIndexStore, SqliteInventoryStore, SqliteLabelStore, SqliteNotificationStore, SqlitePrintQueueStore, SqlitePromptStore, SqlitePublishStore, SqliteRecoveryStore, SqliteReportStore, SqliteSettingStore, SqliteShiftStore, SqliteSystemStore, SqliteTxn, SqliteUserStore, SqliteWorkSessionStore
from vaishnavi.forecast.festivals import load_festivals
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.audit import AuditService
from vaishnavi.services.backup import BackupService
from vaishnavi.services.billing import BillingService
from vaishnavi.services.catalog import CatalogService
from vaishnavi.services.forecast import ForecastService
from vaishnavi.services.hardware_guard import HardwareGuard, discover_printer_names, scanner_probe
from vaishnavi.services.insights import InsightsService
from vaishnavi.services.import_export import ImportExportService
from vaishnavi.services.inventory import InventoryService
from vaishnavi.services.labels import LabelService
from vaishnavi.services.ledger import LedgerService
from vaishnavi.services.notifications import NotificationService
from vaishnavi.services.offers import OfferService
from vaishnavi.services.onboarding import OnboardingService
from vaishnavi.services.open_item_review import OpenItemReviewService
from vaishnavi.services.price_history import PriceHistoryService
from vaishnavi.services.print_queue import PrintQueueService
from vaishnavi.services.publisher import ReportPublisher
from vaishnavi.services.receipt import ReceiptService
from vaishnavi.services.recovery import RecoveryService
from vaishnavi.services.report import ReportService
from vaishnavi.services.secure_settings import SecureSettingsService
from vaishnavi.services.settings import SettingsService
from vaishnavi.services.shift import ShiftService
from vaishnavi.services.user import UserService
from vaishnavi.services.work_session import WorkSessionService

log = logging.getLogger("vaishnavi.app_services")


def build_secret_box(paths: AppPaths) -> SecretBox:
    """Fernet on the OS-keyring master key when possible; ``NullBox`` otherwise (never plaintext)."""
    try:
        from vaishnavi.core.security import FernetBox  # noqa: PLC0415
        from vaishnavi.platform.secrets import load_or_create_master_key  # noqa: PLC0415

        key = load_or_create_master_key(paths.primary)
        box = FernetBox(key.value)
        log.info("SECRET_BOX backend=%s", key.backend)
        return box
    except Exception as exc:  # cryptography missing or key store broken
        log.warning("SECRET_BOX unavailable (%s) — secure settings cannot be written this session code=E-SEC-001", exc)
        return NullBox()


@dataclass
class AppServices:
    """Services wired for one process."""

    settings: Settings
    paths: AppPaths
    config_dir: Path
    bus: EventBus
    txn: SqliteTxn
    audit: AuditService
    users: UserService
    settings_service: SettingsService
    secure_settings: SecureSettingsService
    recovery: RecoveryService
    ledger: LedgerService
    backup: BackupService
    guardian: DbGuardian
    notifications: NotificationService
    sessions: WorkSessionService
    catalog: CatalogService
    prices: PriceHistoryService
    inventory: InventoryService
    labels: LabelService
    import_export: ImportExportService
    onboarding: OnboardingService
    offers: OfferService
    billing: BillingService
    receipts: ReceiptService
    print_queue: PrintQueueService
    hardware: HardwareGuard
    shifts: ShiftService
    review: OpenItemReviewService
    reports: ReportService
    forecast: ForecastService
    insights: InsightsService
    system_store: SqliteSystemStore
    prompts: Any = None
    tools: Any = None
    indexer: Any = None
    chat: Any = None
    publisher: Any = None
    publish_store: Any = None
    purchase_orders: Any = None
    _ai_provider: Any = None
    chips_source: Callable[[], Mapping[str, tuple[str, str]]] = lambda: {}
    """Set by startup to ``lambda: state.chips`` so the assistant's ``shop_status`` tool sees the live chips."""

    @property
    def ai_provider(self) -> Any:
        """The live provider behind a circuit breaker (``.inner`` for the raw one)."""
        return self._ai_provider

    def set_ai_provider(self, provider: Any) -> None:
        """Swap the provider (tests, Settings → Assistant); the breaker and every consumer follow."""
        from vaishnavi.ai.providers.factory import CircuitBreaker  # noqa: PLC0415

        self._ai_provider = provider if isinstance(provider, CircuitBreaker) else CircuitBreaker(provider)
        log.info("AI_PROVIDER_SET provider=%s chat=%s embed=%s", provider.name, provider.chat_model, provider.embed_model)

    def rebuild_ai_provider(self) -> None:
        """Build the provider from the effective settings and the encrypted key."""
        from vaishnavi.ai.providers.factory import build_provider  # noqa: PLC0415

        ai = self.settings_service.effective().ai
        self.set_ai_provider(build_provider(ai, api_key=lambda name: self.secure_settings.reveal_for_system(f"ai.{name}.api_key")))

    @classmethod
    def build(cls, settings: Settings, paths: AppPaths, config_dir: Path, *, box: SecretBox | None = None, event_bus: EventBus = bus, ai_provider: Any = None) -> AppServices:
        """Wire everything."""
        txn = SqliteTxn(paths.database)
        audit = AuditService(SqliteAuditStore())
        setting_store = SqliteSettingStore()
        ledger = LedgerService(SqliteLedgerStore())
        catalog_store = SqliteCatalogStore()
        system_store = SqliteSystemStore()
        secure_box = box if box is not None else build_secret_box(paths)
        catalog = CatalogService(txn, catalog_store, audit, event_bus=event_bus)
        inventory_store = SqliteInventoryStore()
        inventory = InventoryService(txn, inventory_store, catalog_store, audit, event_bus=event_bus)
        settings_service = SettingsService(txn, setting_store, audit, settings, event_bus=event_bus)
        billing_store = SqliteBillingStore()
        offers = OfferService(txn, billing_store, catalog_store, audit)
        billing = BillingService(txn, billing_store, catalog_store, inventory_store, catalog, offers, ledger, audit, settings=settings_service.effective, event_bus=event_bus)
        users = UserService(txn, SqliteUserStore(), audit)
        secure_settings = SecureSettingsService(txn, setting_store, audit, secure_box)
        from vaishnavi.hardware import labels as label_renderers  # noqa: PLC0415 - root wiring may touch the hardware layer
        from vaishnavi.hardware import receipts as receipt_renderers  # noqa: PLC0415
        from vaishnavi.hardware.printer.escpos import transport_for  # noqa: PLC0415
        from vaishnavi.platform.printing import printer_status  # noqa: PLC0415

        def cashier_name(user_id: int) -> str:
            return users.display_names().get(user_id, f"#{user_id}")

        receipts = ReceiptService(txn, billing_store, paths, settings=settings_service.effective, cashier_name=cashier_name, upi_id=lambda: secure_settings.reveal_for_system("payments.upi_id"), renderer=receipt_renderers)
        print_queue = PrintQueueService(txn, SqlitePrintQueueStore(), receipts, audit, settings=settings_service.effective, status=printer_status, transport_factory=transport_for, event_bus=event_bus)
        hardware = HardwareGuard(txn, SqliteHardwareStore(), audit, settings=settings_service.effective, scanner_present=scanner_probe, printer_status=printer_status, discover_printers=discover_printer_names, event_bus=event_bus)
        billing.add_checkout_hook(hardware.can_checkout)
        shifts = ShiftService(txn, SqliteShiftStore(), audit, ledger, event_bus=event_bus)
        reports = ReportService(txn, SqliteReportStore(), ledger, audit)
        forecast = ForecastService(txn, SqliteForecastStore(), catalog_store, settings=settings_service.effective, festivals_loader=lambda: load_festivals(config_dir / settings_service.effective().forecast.festivals_file))

        services = cls(
            settings=settings,
            paths=paths,
            config_dir=config_dir,
            bus=event_bus,
            txn=txn,
            audit=audit,
            users=users,
            settings_service=settings_service,
            secure_settings=secure_settings,
            recovery=RecoveryService(txn, SqliteRecoveryStore(), SqliteUserStore(), audit),
            ledger=ledger,
            backup=BackupService(paths, settings.backup, ledger, event_bus=event_bus, same_device_accepted=settings.locations.same_device),
            guardian=DbGuardian(paths, ledger),
            notifications=NotificationService(txn, SqliteNotificationStore(), event_bus=event_bus),
            sessions=WorkSessionService(txn, SqliteWorkSessionStore(), audit, event_bus=event_bus),
            catalog=catalog,
            prices=PriceHistoryService(txn, catalog_store),
            inventory=inventory,
            labels=LabelService(txn, SqliteLabelStore(), audit, products=catalog_store, renderer=label_renderers),
            import_export=ImportExportService(catalog, inventory),
            onboarding=OnboardingService(txn, system_store, audit),
            offers=offers,
            billing=billing,
            receipts=receipts,
            print_queue=print_queue,
            hardware=hardware,
            shifts=shifts,
            review=OpenItemReviewService(txn, billing_store, catalog, audit),
            reports=reports,
            forecast=forecast,
            insights=InsightsService(reports, forecast, shifts, print_queue),
            system_store=system_store,
        )
        services.notifications.subscribe(event_bus)
        print_queue.subscribe(event_bus)
        _wire_setup_probes(services)
        _wire_assistant(services, ai_provider, settings_service, event_bus)
        _wire_publishing(services, settings_service)
        log.info("SERVICES_READY db=%s provider=%s", paths.database, settings.ai.provider)
        return services

    def effective_settings(self) -> Settings:
        """File settings with the Owner's database overrides applied."""
        return self.settings_service.effective()


def _wire_assistant(c: AppServices, provider: Any, settings_service: SettingsService, event_bus: EventBus) -> None:
    """Provider (breaker) · prompts · tools · help index · chat service; provider follows [ai] setting changes."""
    from vaishnavi.ai.chat_service import ChatService  # noqa: PLC0415 - root wiring may touch the ai layer
    from vaishnavi.ai.indexer import DocIndexer  # noqa: PLC0415
    from vaishnavi.ai.tools import ToolRegistry  # noqa: PLC0415
    from vaishnavi.core.events import SettingsChanged  # noqa: PLC0415
    from vaishnavi.services.prompt import PromptService  # noqa: PLC0415

    if provider is not None:
        c.set_ai_provider(provider)
    else:
        try:
            c.rebuild_ai_provider()
        except Exception as exc:  # noqa: BLE001 - build must not depend on the database being healthy (startup probe runs before the doctor)
            from vaishnavi.ai.providers.null_provider import NullProvider  # noqa: PLC0415

            log.warning("AI_PROVIDER_DEFERRED reason=%s — assistant off until settings can be read", exc)
            c.set_ai_provider(NullProvider(reason_code="E-AI-010", detail="assistant unavailable until the database is ready"))
    c.prompts = PromptService(c.txn, SqlitePromptStore(), c.audit)
    docs_dir = c.config_dir.parent / "docs"
    if not docs_dir.is_dir():
        # installed copy keeps docs next to the package root (src/../docs)
        docs_dir = Path(__file__).resolve().parents[2] / "docs"
    c.indexer = DocIndexer(c.txn, SqliteIndexStore(), docs_dir, provider=lambda: c.ai_provider)
    c.tools = ToolRegistry(reports=c.reports, forecast=c.forecast, catalog=c.catalog, prices=c.prices, backup=c.backup, billing=c.billing, users=c.users, indexer=c.indexer, chips=lambda: c.chips_source(), provider_health=lambda: c.ai_provider.health())
    c.chat = ChatService(c.txn, SqliteChatStore(), provider=lambda: c.ai_provider, tools=c.tools, prompts=c.prompts, notifications=c.notifications, settings=settings_service.effective, indexer=c.indexer)

    def on_settings(e: SettingsChanged) -> None:
        if any(k.startswith("ai.") for k in getattr(e, "keys", ())) and provider is None:
            log.info("AI_SETTINGS_CHANGED keys=%s -> rebuilding provider", ",".join(k for k in e.keys if k.startswith("ai.")))
            c.rebuild_ai_provider()

    event_bus.subscribe(SettingsChanged, on_settings)


def _wire_publishing(c: AppServices, settings_service: SettingsService) -> None:
    """Renderer + outbox + channel factory injected into ``ReportPublisher`` (services never import publishing)."""
    from vaishnavi.publishing import renderer  # noqa: PLC0415 - root wiring may touch the publishing layer
    from vaishnavi.publishing.channels.folder import FolderChannel  # noqa: PLC0415
    from vaishnavi.publishing.channels.smtp import SmtpChannel  # noqa: PLC0415
    from vaishnavi.publishing.channels.telegram import TelegramChannel  # noqa: PLC0415
    from vaishnavi.publishing.channels.whatsapp import WhatsAppChannel  # noqa: PLC0415
    from vaishnavi.publishing.outbox import Outbox  # noqa: PLC0415

    c.publish_store = SqlitePublishStore()
    outbox = Outbox(c.txn, c.publish_store, max_attempts=c.settings.publishing.max_attempts)
    secret = c.secure_settings.reveal_for_system

    def channel_factory(name: str, settings: Settings, publisher: ReportPublisher) -> Any:
        pub = settings.publishing
        if name == "folder":
            return FolderChannel(Path(pub.folder), must_exist=False)
        if name == "email":
            return SmtpChannel(host=pub.smtp_host, port=pub.smtp_port, user=pub.smtp_user, password=secret("publish.smtp_password") or "", sender=pub.email_from, to=pub.email_to, smtp_factory=publisher.smtp_factory)
        if name == "telegram":
            return TelegramChannel(token=secret("publish.telegram_token") or "", chat_id=pub.telegram_chat_id, post=publisher.http_post)
        if name == "whatsapp":
            return WhatsAppChannel(token=secret("publish.whatsapp_token") or "", phone_id=pub.whatsapp_phone_id, to=pub.whatsapp_to, post=publisher.http_post)
        from vaishnavi.core.errors import PublishError  # noqa: PLC0415

        raise PublishError("E-PUB-001", channel=name, detail="unknown channel")

    c.publisher = ReportPublisher(c.txn, outbox=outbox, renderer=renderer, channel_factory=channel_factory, reports=c.reports, forecast=c.forecast, insights=c.insights, shifts=c.shifts, ledger=c.ledger, users=c.users, settings=settings_service.effective, paths=c.paths, notifications=c.notifications, pdf_engine=renderer.default_pdf_engine())
    from vaishnavi.services.purchase_order import PurchaseOrderService  # noqa: PLC0415

    c.purchase_orders = PurchaseOrderService(c.forecast, c.catalog, c.inventory, publisher=c.publisher)


def _wire_setup_probes(c: AppServices) -> None:
    """Data-driven "is this step done?" checks for the first-day checklist (docs/09 §2a)."""

    def shop_profile() -> tuple[bool, str]:
        shop = c.settings_service.effective().shop
        return bool(shop.name.strip() and shop.address_line1.strip() and shop.phone.strip()), shop.name

    def tax() -> tuple[bool, str]:
        t = c.settings_service.effective().tax
        return bool(c.settings_service.overrides().get("tax.registered") is not None or t.registered), f"registered={t.registered}"

    def payments() -> tuple[bool, str]:
        return c.secure_settings.is_set("payments.upi_id"), c.secure_settings.preview("payments.upi_id") or ""

    def staff() -> tuple[bool, str]:
        names = [u for _, u, _, role in c.users.login_tiles() if role.value == "STAFF"]
        return bool(names), ", ".join(names)

    def products() -> tuple[bool, str]:
        total = c.catalog.list_products(limit=1).total
        return total > 0, f"{total} products"

    def labels() -> tuple[bool, str]:
        recent = c.labels.recent(limit=1)
        return bool(recent), ""

    def opening_stock() -> tuple[bool, str]:
        moved = c.inventory.movements(types=("OPENING", "PURCHASE", "STOCK_TAKE"), limit=1)
        return bool(moved), ""

    def scanner() -> tuple[bool, str]:
        hw = c.settings_service.effective().hardware
        return bool(hw.scanner_vid_pid), hw.scanner_vid_pid

    def printer() -> tuple[bool, str]:
        hw = c.settings_service.effective().hardware
        return hw.printer_kind != "none", f"{hw.printer_kind} {hw.printer_address}".strip()

    def backup_drive() -> tuple[bool, str]:
        if c.settings.locations.same_device:
            return True, "same disk accepted at install"
        ready, reason = c.backup.secondary_ready()
        return ready, reason

    def recovery_key() -> tuple[bool, str]:
        return not c.recovery.needs_attention(), ""

    for step, probe in {"shop_profile": shop_profile, "tax": tax, "payments": payments, "staff": staff, "products": products, "labels": labels, "opening_stock": opening_stock, "scanner": scanner, "printer": printer, "backup_drive": backup_drive, "recovery_key": recovery_key}.items():
        c.onboarding.register_probe(step, probe)
