"""Seed the reference rows a fresh database needs (docs/04 §5) — idempotent, plain SQL, stdlib only.

Why plain SQL and not the ORM: the seed must run from the installer's database stage, must be testable
with ``sqlite3`` on any machine, and must produce exactly the same rows whether Alembic or a test created
the schema. Every insert is guarded by an existence probe, so running the seed twice (re-install,
restored copy, upgrade) changes nothing — the returned report says what was actually inserted.

What is seeded
* roles ``OWNER`` / ``STAFF``;
* the Owner account with the password **sentinel** ``!SETUP`` and ``must_change_password=1`` — login
  refuses the sentinel (``E-AUTH-005``) until ``UserService.complete_owner_setup`` runs on the shop PC,
  so nobody can claim the account by guessing a default password;
* ten puja-shop categories (EN + HI);
* the system product ``OPEN-ITEM`` (``is_system=1``; free-text lines are stored against it, it never moves stock);
* ``setting`` rows for the unit list and the tax presets (presets to pick from — never a guessed rate per product);
* ``counter`` rows (invoice sequence, product id, held carts);
* the eleven onboarding ``setup_step`` rows (all undone);
* ``prompt_version`` 1 with the default Owner instructions (docs/06 §3);
* ``db_identity`` (instance id, versions) when the table is empty.
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from vaishnavi import __version__
from vaishnavi.core.clock import iso_utc
from vaishnavi.db.records import PASSWORD_SENTINEL
from vaishnavi.db.schema import SCHEMA_VERSION

log = logging.getLogger("vaishnavi.db.seed")

OWNER_USERNAME: Final = "owner"
OWNER_DISPLAY_NAME: Final = "Owner"
"""The password column holds :data:`PASSWORD_SENTINEL` (not a hash) until Owner setup runs on the shop PC."""

OPEN_ITEM_SKU: Final = "OPEN-ITEM"
OPEN_ITEM_NAME: Final = "Open item (no barcode)"

ROLES: Final[tuple[tuple[int, str], ...]] = ((1, "OWNER"), (2, "STAFF"))

CATEGORIES: Final[tuple[tuple[str, str, str], ...]] = (
    ("Agarbatti & Dhoop", "अगरबत्ती और धूप", "incense"),
    ("Camphor & Ghee", "कपूर और घी", "camphor"),
    ("Oil & Wicks", "तेल और बाती", "oil"),
    ("Deity Clothes", "भगवान के वस्त्र", "clothes"),
    ("Idols & Frames", "मूर्तियाँ और फ़्रेम", "idol"),
    ("Puja Thali & Metalware", "पूजा थाली और धातु सामान", "thali"),
    ("Colours & Kumkum", "रंग और कुमकुम", "kumkum"),
    ("Flowers & Garlands (artificial)", "फूल और मालाएँ (कृत्रिम)", "garland"),
    ("Threads & Malas", "धागे और मालाएँ", "mala"),
    ("Decoration", "सजावट", "decoration"),
    ("Books & Yantras", "पुस्तकें और यंत्र", "book"),
    ("Prasad & Dry Fruits", "प्रसाद और मेवे", "prasad"),
    ("Festival Specials", "त्योहार विशेष", "festival"),
    ("Others", "अन्य", "other"),
)

UNITS: Final[tuple[dict[str, Any], ...]] = (
    {"label": "pc", "base_unit": "pcs", "factor": 1, "sale_mode": "PIECE"},
    {"label": "pkt", "base_unit": "pcs", "factor": 1, "sale_mode": "PIECE"},
    {"label": "box", "base_unit": "pcs", "factor": 1, "sale_mode": "PIECE"},
    {"label": "dozen", "base_unit": "pcs", "factor": 12, "sale_mode": "PIECE"},
    {"label": "g", "base_unit": "g", "factor": 1, "sale_mode": "WEIGHT"},
    {"label": "100 g", "base_unit": "g", "factor": 100, "sale_mode": "WEIGHT"},
    {"label": "kg", "base_unit": "g", "factor": 1000, "sale_mode": "WEIGHT"},
    {"label": "ml", "base_unit": "ml", "factor": 1, "sale_mode": "VOLUME"},
    {"label": "l", "base_unit": "ml", "factor": 1000, "sale_mode": "VOLUME"},
    {"label": "cm", "base_unit": "cm", "factor": 1, "sale_mode": "LENGTH"},
    {"label": "m", "base_unit": "cm", "factor": 100, "sale_mode": "LENGTH"},
)

TAX_PRESETS: Final[tuple[dict[str, Any], ...]] = (
    {"label": "Exempt / not registered", "rate_bp": 0},
    {"label": "GST 5%", "rate_bp": 500},
    {"label": "GST 12%", "rate_bp": 1200},
    {"label": "GST 18%", "rate_bp": 1800},
    {"label": "GST 28%", "rate_bp": 2800},
)

SETUP_STEPS: Final[tuple[str, ...]] = (
    "shop_profile",
    "tax",
    "payments",
    "staff",
    "products",
    "labels",
    "opening_stock",
    "scanner",
    "printer",
    "backup_drive",
    "recovery_key",
)

COUNTERS: Final[tuple[str, ...]] = ("invoice", "product_id", "held_cart")

DEFAULT_OWNER_INSTRUCTIONS: Final = (
    "Your name is Sahayak. Be brief and friendly. Our shop sells puja samagri. Default language Hinglish. "
    "When a forecast tool returns caveats, state them. Format money as ₹ with Indian grouping. "
    "If the user's role does not permit a tool, say the owner can see this."
)
PROMPT_CORE_HASH_PENDING: Final = "pending"
"""``PromptService`` stamps the real locked-core hash on first start; the seed cannot know it."""


class DbapiLike(Protocol):
    """The slice of a DB-API connection the seed needs (``sqlite3.Connection`` or SQLAlchemy's raw connection)."""

    def execute(self, sql: str, parameters: Any = ..., /) -> Any:  # pragma: no cover - protocol
        """Run one statement and return a cursor."""
        ...

    def commit(self) -> None:  # pragma: no cover - protocol
        """Commit."""
        ...


@dataclass
class SeedReport:
    """What :func:`apply_seed` inserted (empty → the database was already seeded)."""

    inserted: dict[str, list[str]] = field(default_factory=dict)
    owner_created: bool = False
    instance_id: str = ""

    def add(self, table: str, key: str) -> None:
        """Record one inserted row."""
        self.inserted.setdefault(table, []).append(key)

    @property
    def total(self) -> int:
        """Number of rows inserted."""
        return sum(len(v) for v in self.inserted.values())


# ----------------------------------------------------------------------------- helpers
def _insert_if_missing(
    conn: DbapiLike,
    report: SeedReport,
    table: str,
    key: str,
    probe: str,
    probe_params: tuple[Any, ...],
    insert: str,
    insert_params: tuple[Any, ...],
) -> bool:
    if conn.execute(probe, probe_params).fetchone() is not None:
        return False
    conn.execute(insert, insert_params)
    report.add(table, key)
    log.debug("SEED_INSERT table=%s key=%s", table, key)
    return True


# ----------------------------------------------------------------------------- the seed
def apply_seed(conn: DbapiLike, *, host: str | None = None, now: str | None = None) -> SeedReport:
    """Insert every missing reference row; safe to call on every start. Commits at the end."""
    report = SeedReport()
    at = now or iso_utc()
    log.info("SEED_START schema_version=%s app_version=%s", SCHEMA_VERSION, __version__)

    for role_id, name in ROLES:
        _insert_if_missing(conn, report, "role", name, "SELECT 1 FROM role WHERE name = ?", (name,), "INSERT INTO role(id, name, permissions) VALUES (?, ?, '{}')", (role_id, name))

    report.owner_created = _insert_if_missing(
        conn,
        report,
        "user",
        OWNER_USERNAME,
        "SELECT 1 FROM user WHERE role_id = (SELECT id FROM role WHERE name = 'OWNER')",
        (),
        "INSERT INTO user(username, display_name, role_id, password_hash, must_change_password, created_at) "
        "VALUES (?, ?, (SELECT id FROM role WHERE name = 'OWNER'), ?, 1, ?)",
        (OWNER_USERNAME, OWNER_DISPLAY_NAME, PASSWORD_SENTINEL, at),
    )

    for order, (name, name_hi, icon) in enumerate(CATEGORIES, start=1):
        _insert_if_missing(conn, report, "category", name, "SELECT 1 FROM category WHERE name = ?", (name,), "INSERT INTO category(name, name_hi, sort_order, icon) VALUES (?, ?, ?, ?)", (name, name_hi, order * 10, icon))

    _insert_if_missing(
        conn,
        report,
        "product",
        OPEN_ITEM_SKU,
        "SELECT 1 FROM product WHERE sku = ?",
        (OPEN_ITEM_SKU,),
        "INSERT INTO product(sku, name, name_hi, price_paise, is_system, quick_pick, is_active, created_at, updated_at) VALUES (?, ?, ?, 0, 1, 0, 1, ?, ?)",
        (OPEN_ITEM_SKU, OPEN_ITEM_NAME, "खुला आइटम (बिना बारकोड)", at, at),
    )

    for key, value in (("catalog.units", list(UNITS)), ("tax.presets", list(TAX_PRESETS))):
        _insert_if_missing(conn, report, "setting", key, "SELECT 1 FROM setting WHERE key = ?", (key,), "INSERT INTO setting(key, value, type, updated_at) VALUES (?, ?, 'json', ?)", (key, json.dumps(value, ensure_ascii=False), at))

    for name in COUNTERS:
        _insert_if_missing(conn, report, "counter", name, "SELECT 1 FROM counter WHERE name = ?", (name,), "INSERT INTO counter(name, value) VALUES (?, 0)", (name,))

    for step in SETUP_STEPS:
        _insert_if_missing(conn, report, "setup_step", step, "SELECT 1 FROM setup_step WHERE step = ?", (step,), "INSERT INTO setup_step(step) VALUES (?)", (step,))

    _insert_if_missing(
        conn,
        report,
        "prompt_version",
        "1",
        "SELECT 1 FROM prompt_version WHERE version = 1",
        (),
        "INSERT INTO prompt_version(version, editable_instructions, core_hash, default_language, is_active, redteam_passed, created_at, note) "
        "VALUES (1, ?, ?, 'hinglish', 1, 1, ?, 'default instructions (seed)')",
        (DEFAULT_OWNER_INSTRUCTIONS, PROMPT_CORE_HASH_PENDING, at),
    )

    row = conn.execute("SELECT instance_id FROM db_identity LIMIT 1").fetchone()
    if row is None:
        instance_id = secrets.token_hex(8)
        # last_open_at stays NULL: the app's guardian stamps it on open; setting it here would make the
        # very next start look like an unclean close.
        conn.execute(
            "INSERT INTO db_identity(instance_id, created_at, app_version, schema_version) VALUES (?, ?, ?, ?)",
            (instance_id, at, __version__, SCHEMA_VERSION),
        )
        report.add("db_identity", instance_id)
        log.info("SEED_IDENTITY_CREATED instance_id=%s host=%s", instance_id, host or socket.gethostname())
    else:
        instance_id = str(row[0])
    report.instance_id = instance_id

    conn.commit()
    log.info("SEED_DONE inserted=%d owner_created=%s tables=%s", report.total, report.owner_created, ",".join(sorted(report.inserted)) or "-")
    return report


def is_owner_setup_pending(conn: DbapiLike) -> bool:
    """True while the Owner account still carries the password sentinel (first-run wizard must run)."""
    row = conn.execute(
        "SELECT 1 FROM user WHERE password_hash = ? AND role_id = (SELECT id FROM role WHERE name = 'OWNER')",
        (PASSWORD_SENTINEL,),
    ).fetchone()
    return row is not None
