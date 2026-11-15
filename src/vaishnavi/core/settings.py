"""Typed application settings from ``config/settings.toml`` + environment + database overrides.

Stdlib only (dataclasses + ``tomllib``) so the installer's doctor can read settings before any
package is installed and the core test tier covers it (ADR-009, note in ADR-040).

Precedence, lowest to highest: **defaults → settings.toml → environment → database overrides**.

* Environment: ``VAISHNAVI_<SECTION>__<KEY>`` (double underscore), e.g. ``VAISHNAVI_SERVER__PORT=8090``.
* Database overrides: the Owner-editable ``setting`` table supplies a flat ``{"section.key": value}``
  map; ``Settings.with_overrides()`` applies it (typed, validated) without re-reading files.

Safety rules enforced here:
* a secret (``api_key``, ``token``, ``password``) inside **any** TOML section → ``E-SEC-006``, start refused;
* a required key missing or out of range → ``E-CFG-001`` with the dotted key name for the owner.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Final, Literal, get_args, get_origin, get_type_hints

from vaishnavi.core.errors import ConfigError, SecretsError

ENV_PREFIX: Final = "VAISHNAVI_"
SETTINGS_FILE: Final = "settings.toml"
LOCATIONS_FILE: Final = "locations.toml"
FORBIDDEN_KEYS: Final = frozenset({"api_key", "apikey", "token", "password", "secret", "upi_pin"})


# ----------------------------------------------------------------------------- sections
@dataclass
class ShopSettings:
    """Who the shop is — printed on every receipt."""

    name: str = "Vaishnavi Puja Store"
    name_hi: str = "वैष्णवी पूजा स्टोर"
    address_line1: str = ""
    address_line2: str = ""
    phone: str = ""
    email: str = ""
    gstin: str = ""
    state_code: str = ""
    currency: str = "INR"
    language: Literal["en", "hi", "hinglish"] = "en"
    """Default UI language; each user may switch in the drawer."""


@dataclass
class TaxSettings:
    """GST behaviour (rates per product; these are the shop-wide rules)."""

    registered: bool = False
    """False → no GST lines on receipts (composition / unregistered); True → CGST+SGST shown."""
    prices_include_tax: bool = True
    cgst_label: str = "CGST"
    sgst_label: str = "SGST"
    default_rate_bp: int = 0
    """Basis points (1200 = 12 %) applied to new products unless changed."""


@dataclass
class ReceiptSettings:
    """Receipt layout."""

    paper_width_mm: Literal[58, 80] = 80
    header_lines: list[str] = field(default_factory=list)
    footer_lines: list[str] = field(default_factory=lambda: ["Thank you — Dhanyavaad!"])
    show_hindi_name: bool = False
    show_qr_upi: bool = False
    copies: int = 1
    invoice_prefix: str = "VPS"


@dataclass
class BillingSettings:
    """Anti-theft and counter policies (docs/05)."""

    require_scan: bool = True
    require_printer: bool = False
    manual_add_policy: Literal["ALLOW", "PIN", "BLOCK"] = "ALLOW"
    """Open-item lines for staff: ALLOW (default — FR-D2a; capped, marked on the receipt, reported) | PIN | BLOCK."""
    open_item_cap_per_bill: int = 3
    adhoc_discount_cap_percent: int = 10
    negative_stock_policy: Literal["BLOCK", "WARN", "ALLOW"] = "BLOCK"
    reprint_limit: int = 2
    void_window_hours: int = 24
    """Voids/refunds allowed this long after the sale without special approval (docs/05 §7)."""
    tax_rounding: Literal["NEAREST_RUPEE", "NONE"] = "NEAREST_RUPEE"
    """Legal "round-off" line to the nearest rupee, or exact paise."""
    idle_lock_minutes: int = 10
    heartbeat_seconds: int = 15
    """Browser heartbeat for attendance (docs/05 §6a)."""


@dataclass
class ServerSettings:
    """Where the app listens."""

    host: str = "127.0.0.1"
    port: int = 8080
    https_port: int = 8443
    https_enabled: bool = False
    """True → also serve HTTPS on ``https_port`` with a self-signed certificate (phones on the shop Wi-Fi)."""
    lan_access: bool = False
    """True → bind 0.0.0.0 so phones/tablets on the shop Wi-Fi can open the app (and the log console)."""
    kiosk_browser: bool = True

    @property
    def bind_host(self) -> str:
        """Effective bind address."""
        return "0.0.0.0" if self.lan_access else self.host  # noqa: S104 (owner opt-in)


@dataclass
class OllamaSettings:
    """Local provider."""

    host: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen2.5:3b-instruct"
    embed_model: str = "nomic-embed-text"
    keep_alive: str = "30m"
    num_ctx: int = 8192


@dataclass
class CloudProviderSettings:
    """OpenAI or Hugging Face (the key is NOT here — ``secure_setting``)."""

    base_url: str = ""
    chat_model: str = ""
    embed_model: str = ""


@dataclass
class CloudBudgetSettings:
    """Monthly spend guard for cloud providers."""

    monthly_token_budget: int = 2_000_000
    budget_alert_pct: int = 80


@dataclass
class AiSettings:
    """Assistant configuration (docs/06 §2)."""

    enabled: bool = True
    provider: Literal["ollama", "openai", "huggingface", "none"] = "ollama"
    default_language: Literal["english", "hindi", "hinglish"] = "hinglish"
    temperature: float = 0.1
    top_k_docs: int = 4
    max_tool_rounds: int = 4
    request_timeout_s: int = 60
    max_messages_per_minute: int = 10
    max_message_chars: int = 500
    flag_alert_threshold: int = 2
    off_topic_limit_per_session: int = 3
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    openai: CloudProviderSettings = field(default_factory=lambda: CloudProviderSettings("https://api.openai.com/v1", "gpt-4o-mini", "text-embedding-3-small"))
    huggingface: CloudProviderSettings = field(default_factory=lambda: CloudProviderSettings("https://router.huggingface.co/v1", "Qwen/Qwen2.5-7B-Instruct", "BAAI/bge-m3"))
    cloud: CloudBudgetSettings = field(default_factory=CloudBudgetSettings)


@dataclass
class BackupSettings:
    """Snapshot schedule (docs/14)."""

    nightly_time: str = "21:30"
    interval_minutes: int = 0
    """0 = only on start/shift-close/nightly; >0 adds an interval snapshot."""
    keep_daily: int = 30
    keep_weekly: int = 12
    keep_monthly: int = 24
    restore_drill_weekday: int = 6
    """0 = Monday … 6 = Sunday."""


@dataclass
class ForecastSettings:
    """Demand forecasting (docs/10 §11)."""

    min_history_days: int = 28
    """Below this the assistant and the planner say "not enough data yet" (E-FCST-001)."""
    horizon_days: int = 14
    lead_days: int = 3
    """Supplier lead time used by the reorder plan."""
    cover_days: int = 7
    """Days of stock to hold after a delivery."""
    ewma_alpha: float = 0.3
    festivals_file: str = "festivals.toml"


@dataclass
class HardwareSettings:
    """Paired devices."""

    scanner_vid_pid: str = ""
    """'VID:PID' of the paired barcode scanner; empty until the onboarding wizard pairs one."""
    scanner_suffix: Literal["enter", "tab", "none"] = "enter"
    printer_kind: Literal["none", "usb", "network", "system", "file"] = "none"
    printer_address: str = ""
    """USB 'VID:PID', network 'host:9100', OS printer name, or a file path for ``file`` (testing without a printer)."""
    scale_port: str = ""
    scale_barcode_format: str = ""
    """Layout of weight/price-embedded EAN-13 codes from a label scale, e.g. ``21PPPPPWWWWWC`` (empty = none)."""


@dataclass
class LocationSettings:
    """Primary and secondary locations (from ``config/locations.toml``, written by the installer)."""

    primary: str = ""
    secondary: str = ""
    same_device: bool = False

    @property
    def primary_path(self) -> Path:
        """Primary as a path (``~/VaishnaviStore`` when unset)."""
        return Path(self.primary) if self.primary else Path.home() / "VaishnaviStore"

    @property
    def secondary_path(self) -> Path:
        """Secondary as a path (``~/VaishnaviStore-Backups`` when unset)."""
        return Path(self.secondary) if self.secondary else Path.home() / "VaishnaviStore-Backups"


@dataclass
class PublishingSettings:
    """Remote report publishing (docs/05 §11) — opt-in; credentials live in ``secure_setting``."""

    enabled: bool = False
    channels: list[str] = field(default_factory=list)
    """Any of ``folder`` · ``email`` · ``telegram`` · ``whatsapp``."""
    folder: str = ""
    """Synced folder (OneDrive / Google Drive) that receives the PDFs/HTML."""
    email_to: str = ""
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    telegram_chat_id: str = ""
    whatsapp_phone_id: str = ""
    whatsapp_to: str = ""
    digest_time: str = "21:35"
    weekly_weekday: int = 6
    """0 = Monday … 6 = Sunday."""
    max_attempts: int = 10


@dataclass
class Settings:
    """The whole configuration."""

    shop: ShopSettings = field(default_factory=ShopSettings)
    tax: TaxSettings = field(default_factory=TaxSettings)
    receipt: ReceiptSettings = field(default_factory=ReceiptSettings)
    billing: BillingSettings = field(default_factory=BillingSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    ai: AiSettings = field(default_factory=AiSettings)
    backup: BackupSettings = field(default_factory=BackupSettings)
    hardware: HardwareSettings = field(default_factory=HardwareSettings)
    forecast: ForecastSettings = field(default_factory=ForecastSettings)
    publishing: PublishingSettings = field(default_factory=PublishingSettings)
    locations: LocationSettings = field(default_factory=LocationSettings)
    source_files: list[str] = field(default_factory=list, repr=False)

    # -- loading --------------------------------------------------------------------------------
    @classmethod
    def load(cls, config_dir: Path, *, environ: dict[str, str] | None = None) -> Settings:
        """defaults ← settings.toml ← locations.toml ← environment; validated."""
        settings = cls()
        env = os.environ if environ is None else environ
        settings_path = config_dir / SETTINGS_FILE
        if settings_path.exists():
            data = _read_toml(settings_path)
            _reject_secrets(data, settings_path)
            _apply_tree(settings, data, source=str(settings_path))
            settings.source_files.append(str(settings_path))
        locations_path = config_dir / LOCATIONS_FILE
        if locations_path.exists():
            data = _read_toml(locations_path)
            _apply_tree(settings, data, source=str(locations_path))
            settings.source_files.append(str(locations_path))
        _apply_env(settings, env)
        settings.validate()
        return settings

    def with_overrides(self, overrides: dict[str, Any]) -> Settings:
        """A copy with ``{"section.key": value}`` (database ``setting`` rows) applied and validated."""
        clone = dataclasses.replace(self)  # shallow — replace nested sections below
        for name in (f.name for f in fields(Settings) if f.name != "source_files"):
            setattr(clone, name, _deepcopy_dc(getattr(self, name)))
        for dotted, value in overrides.items():
            _set_dotted(clone, dotted, value, source="database")
        clone.validate()
        return clone

    def as_dict(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        """Plain dict (for the API / Settings screen). There are no secrets here by design."""
        return {f.name: dataclasses.asdict(getattr(self, f.name)) for f in fields(Settings) if f.name != "source_files"}

    # -- validation -----------------------------------------------------------------------------
    def validate(self) -> None:
        """Cross-field rules that types alone cannot express. Raises ``ConfigError`` (E-CFG-001)."""
        if not self.shop.name.strip():
            raise ConfigError("E-CFG-001", key="shop.name")
        if not 1 <= self.server.port <= 65535 or not 1 <= self.server.https_port <= 65535:
            raise ConfigError("E-CFG-001", key="server.port", detail=f"port={self.server.port} https={self.server.https_port}")
        if self.server.port == self.server.https_port:
            raise ConfigError("E-CFG-001", key="server.https_port", detail="must differ from server.port")
        if not 0 <= self.billing.adhoc_discount_cap_percent <= 100:
            raise ConfigError("E-CFG-001", key="billing.adhoc_discount_cap_percent")
        if not 0 <= self.tax.default_rate_bp <= 10_000:
            raise ConfigError("E-CFG-001", key="tax.default_rate_bp")
        if self.tax.registered and not self.shop.gstin.strip():
            raise ConfigError("E-CFG-001", key="shop.gstin", detail="tax.registered is true")
        if not 0 <= self.ai.cloud.budget_alert_pct <= 100:
            raise ConfigError("E-CFG-001", key="ai.cloud.budget_alert_pct")
        if self.billing.heartbeat_seconds < 5:
            raise ConfigError("E-CFG-001", key="billing.heartbeat_seconds", detail="minimum 5")
        hh, _, mm = self.backup.nightly_time.partition(":")
        if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
            raise ConfigError("E-CFG-001", key="backup.nightly_time", detail=self.backup.nightly_time)
        if self.ai.provider in ("openai", "huggingface"):
            cloud = getattr(self.ai, self.ai.provider)
            if not cloud.base_url or not cloud.chat_model:
                raise ConfigError("E-CFG-001", key=f"ai.{self.ai.provider}.chat_model")
        bad = [c for c in self.publishing.channels if c not in ("folder", "email", "telegram", "whatsapp")]
        if bad:
            raise ConfigError("E-CFG-001", key="publishing.channels", detail=f"unknown channel(s): {', '.join(bad)}")
        if not 0 <= self.publishing.weekly_weekday <= 6:
            raise ConfigError("E-CFG-001", key="publishing.weekly_weekday")
        hh, _, mm = self.publishing.digest_time.partition(":")
        if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
            raise ConfigError("E-CFG-001", key="publishing.digest_time", detail=self.publishing.digest_time)


# ----------------------------------------------------------------------------- internals
def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError("E-CFG-001", key=path.name, detail=f"unreadable: {exc}", cause=exc) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("E-CFG-001", key=path.name, detail=f"invalid TOML: {exc}", cause=exc) from exc


def _reject_secrets(tree: dict[str, Any], path: Path, prefix: str = "") -> None:
    for key, value in tree.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            _reject_secrets(value, path, f"{dotted}.")
        elif key.lower() in FORBIDDEN_KEYS and str(value).strip():
            raise SecretsError("E-SEC-006", path=str(path), detail=f"key {dotted}")


def _apply_tree(settings: Settings, tree: dict[str, Any], *, source: str) -> None:
    for section, values in tree.items():
        if not isinstance(values, dict):
            continue  # unknown top-level scalar: ignored (forward compatibility)
        if not hasattr(settings, section) or section == "source_files":
            continue
        _apply_section(getattr(settings, section), values, prefix=section, source=source)


def _hints(target: Any) -> dict[str, Any]:
    """Resolved field types of a dataclass instance (annotations are strings under PEP 563)."""
    return get_type_hints(type(target))


def _apply_section(target: Any, values: dict[str, Any], *, prefix: str, source: str) -> None:
    names = {f.name for f in fields(target)}
    hints = _hints(target)
    for key, value in values.items():
        if key not in names:
            continue  # unknown key: ignored, so an older app can read a newer file
        current = getattr(target, key)
        if dataclasses.is_dataclass(current) and isinstance(value, dict):
            _apply_section(current, value, prefix=f"{prefix}.{key}", source=source)
        else:
            setattr(target, key, _coerce(hints[key], value, f"{prefix}.{key}", source))


def _set_dotted(settings: Settings, dotted: str, value: Any, *, source: str) -> None:
    parts = dotted.split(".")
    target: Any = settings
    for part in parts[:-1]:
        if not hasattr(target, part) or not dataclasses.is_dataclass(getattr(target, part)):
            raise ConfigError("E-CFG-001", key=dotted, detail=f"unknown section ({source})")
        target = getattr(target, part)
    names = {f.name for f in fields(target)}
    if parts[-1] not in names:
        raise ConfigError("E-CFG-001", key=dotted, detail=f"unknown key ({source})")
    setattr(target, parts[-1], _coerce(_hints(target)[parts[-1]], value, dotted, source))


def _apply_env(settings: Settings, env: dict[str, str]) -> None:
    for name, raw in env.items():
        if not name.startswith(ENV_PREFIX) or "__" not in name:
            continue
        dotted = name[len(ENV_PREFIX):].lower().replace("__", ".")
        try:
            _set_dotted(settings, dotted, raw, source=f"environment {name}")
        except ConfigError:
            # Unknown env names are ignored on purpose (other tools may use the prefix).
            continue


def _coerce(ann: Any, value: Any, dotted: str, source: str) -> Any:
    """Convert a TOML/env/DB value to the field's declared type; raise E-CFG-001 when impossible."""
    origin = get_origin(ann)
    try:
        if origin is Literal:
            allowed = get_args(ann)
            candidate: Any = value
            if all(isinstance(a, int) for a in allowed) and isinstance(value, str) and value.isdigit():
                candidate = int(value)
            if candidate not in allowed:
                raise ValueError(f"must be one of {allowed}")
            return candidate
        if ann is bool:
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no", "off"):
                return False
            raise ValueError("expected true/false")
        if ann is int:
            if isinstance(value, bool):
                raise ValueError("expected a number")
            return int(value)
        if ann is float:
            return float(value)
        if ann is str:
            return str(value)
        if origin is list or ann is list:
            if isinstance(value, str):
                return [v.strip() for v in value.split("|") if v.strip()]
            return [str(v) for v in value]
    except (TypeError, ValueError) as exc:
        raise ConfigError("E-CFG-001", key=dotted, detail=f"{exc} ({source}, got {value!r})") from exc
    return value


def _deepcopy_dc(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.replace(obj, **{f.name: _deepcopy_dc(getattr(obj, f.name)) for f in fields(obj)})
    if isinstance(obj, list):
        return list(obj)
    return obj


# ----------------------------------------------------------------------------- dotted-key helpers
def known_keys() -> frozenset[str]:
    """Every dotted leaf key of :class:`Settings` (``shop.name``, ``ai.ollama.chat_model`` …)."""
    found: list[str] = []

    def walk(target: Any, prefix: str) -> None:
        for f in fields(target):
            if f.name == "source_files":
                continue
            value = getattr(target, f.name)
            dotted = f"{prefix}{f.name}"
            if dataclasses.is_dataclass(value):
                walk(value, f"{dotted}.")
            else:
                found.append(dotted)

    walk(Settings(), "")
    return frozenset(found)


def get_dotted(settings: Settings, dotted: str) -> Any:
    """Value of a dotted key; ``ConfigError`` (E-CFG-001) when the key does not exist."""
    target: Any = settings
    for part in dotted.split("."):
        if not dataclasses.is_dataclass(target) or not hasattr(target, part):
            raise ConfigError("E-CFG-001", key=dotted, detail="unknown key")
        target = getattr(target, part)
    return target
