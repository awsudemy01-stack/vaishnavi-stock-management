"""Application logging: files per family, rotation, JSON lines, correlation ids, redaction (stdlib only).

Set up **before anything else** (docs/08 §1). Layout under ``<logs_dir>`` (docs/08 §2)::

    app/vaishnavi.log  (DEBUG, daily, 30 d)   app/vaishnavi.jsonl (same events as JSON lines)
    errors/errors.log  (WARNING+, 90 d)        audit/audit.jsonl   (365 d, mirror of the DB table)
    ai/ai.log (14 d)   hardware/hardware.log (14 d)   access/http.log (7 d)

Every record carries ``cid`` (correlation id), ``user``/``role`` and ``shift`` from ``ContextVar``s
so one UI action, its service calls, SQL and job runs line up in the log. Writing happens on a
``QueueListener`` thread so a slow disk never delays a sale. Credential-like text is masked.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import queue
import re
import sys
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from vaishnavi.core.clock import IST
from vaishnavi.core.ids import new_ulid

ROOT_LOGGER: Final = "vaishnavi"

# ----------------------------------------------------------------------------- context
_cid: ContextVar[str] = ContextVar("vaishnavi_cid", default="-")
_user: ContextVar[str] = ContextVar("vaishnavi_user", default="-")
_role: ContextVar[str] = ContextVar("vaishnavi_role", default="-")
_shift: ContextVar[str] = ContextVar("vaishnavi_shift", default="-")


def current_cid() -> str:
    """Correlation id of the current request/job ('-' when none)."""
    return _cid.get()


def new_cid() -> str:
    """Create and install a fresh correlation id (returns it)."""
    cid = new_ulid()
    _cid.set(cid)
    return cid


@contextmanager
def with_correlation(cid: str | None = None, *, user: str | None = None, role: str | None = None, shift: int | str | None = None) -> Iterator[str]:
    """Scope a correlation id (and optional user context) around a request, UI action or job."""
    tokens = [_cid.set(cid or new_ulid())]
    if user is not None:
        tokens.append(_user.set(user))
    if role is not None:
        tokens.append(_role.set(role))
    if shift is not None:
        tokens.append(_shift.set(str(shift)))
    try:
        yield _cid.get()
    finally:
        for var, token in zip((_cid, _user, _role, _shift)[: len(tokens)], tokens, strict=False):
            var.reset(token)


def set_user_context(user: str | None, role: str | None, shift: int | str | None = None) -> None:
    """Set who is acting for the rest of this context (login)."""
    _user.set(user or "-")
    _role.set(role or "-")
    _shift.set(str(shift) if shift is not None else "-")


# ----------------------------------------------------------------------------- filters
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\b(api[_-]?key|token|password|passwd|pwd|secret|authorization|bearer|pin|otp|recovery[_-]?key)(\s*[=:]\s*|\s+)(['\"]?)((?:bearer\s+)?[^'\"\s,;]+)\3"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})"),
    re.compile(r"\b(hf_[A-Za-z0-9]{8,})"),
)
MASK: Final = "***"
SENSITIVE_EXTRA_KEYS: Final = frozenset({"pin", "password", "password_hash", "secret", "api_key", "token", "recovery_key", "otp", "card", "account_no", "ifsc", "upi_id"})


def redact(text: str) -> str:
    """Mask credential-like values in free text."""
    out = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    out = _SECRET_PATTERNS[1].sub(MASK, out)
    return _SECRET_PATTERNS[2].sub(MASK, out)


class ContextFilter(logging.Filter):
    """Adds cid/user/role/shift to every record and redacts the message + sensitive extras."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.cid = getattr(record, "cid", None) or _cid.get()
        record.user = getattr(record, "user", None) or _user.get()
        record.role = getattr(record, "role", None) or _role.get()
        record.shift = getattr(record, "shift", None) or _shift.get()
        try:
            rendered = record.getMessage()
        except (TypeError, ValueError):
            rendered = str(record.msg)
        record.msg = redact(rendered)
        record.args = ()
        for key in SENSITIVE_EXTRA_KEYS:
            if key in record.__dict__:
                record.__dict__[key] = MASK
        return True


# ----------------------------------------------------------------------------- formatters
_STANDARD_ATTRS: Final = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {"cid", "user", "role", "shift", "message", "asctime", "taskName"}


def extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Structured fields passed via ``extra={...}`` (everything that is not a standard attribute)."""
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS and not k.startswith("_")}


class KeyValueFormatter(logging.Formatter):
    """Human text: ``2026-11-14 10:42:17.318 IST | INFO  | logger | cid=… user=…(ROLE) shift=… | MSG k=v``."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S") + f".{int(record.msecs):03d} IST"
        ctx = f"cid={record.cid} user={record.user}({record.role}) shift={record.shift}"  # type: ignore[attr-defined]
        kv = " ".join(f"{k}={_scalar(v)}" for k, v in extra_fields(record).items())
        line = f"{ts} | {record.levelname:<5} | {record.name} | {ctx} | {record.getMessage()}" + (f" {kv}" if kv else "")
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line; ``event`` is the message, extras become top-level keys."""

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "lvl": record.levelname,
            "logger": record.name,
            "cid": record.cid,  # type: ignore[attr-defined]
            "user": record.user,  # type: ignore[attr-defined]
            "role": record.role,  # type: ignore[attr-defined]
            "shift": record.shift,  # type: ignore[attr-defined]
            "event": record.getMessage(),
        }
        doc.update({k: _jsonable(v) for k, v in extra_fields(record).items()})
        if record.exc_info:
            doc["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(doc, ensure_ascii=False, default=str)


def _scalar(value: Any) -> str:
    text = str(value)
    return f'"{text}"' if " " in text else text


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, dict)):
        return value
    return str(value)


# ----------------------------------------------------------------------------- configuration
@dataclass
class FamilySpec:
    """One log family (file + retention + what goes into it)."""

    folder: str
    filename: str
    level: int
    backup_days: int
    logger_prefixes: tuple[str, ...] = ()
    """Only records from these logger names (empty → all)."""
    json_lines: bool = False


DEFAULT_FAMILIES: Final[dict[str, FamilySpec]] = {
    "app": FamilySpec("app", "vaishnavi.log", logging.DEBUG, 30),
    "app_json": FamilySpec("app", "vaishnavi.jsonl", logging.DEBUG, 30, json_lines=True),
    "errors": FamilySpec("errors", "errors.log", logging.WARNING, 90),
    "audit": FamilySpec("audit", "audit.jsonl", logging.INFO, 365, ("vaishnavi.audit",), json_lines=True),
    "ai": FamilySpec("ai", "ai.log", logging.DEBUG, 14, ("vaishnavi.ai",)),
    "hardware": FamilySpec("hardware", "hardware.log", logging.DEBUG, 14, ("vaishnavi.hardware", "vaishnavi.platform")),
    "access": FamilySpec("access", "http.log", logging.INFO, 7, ("uvicorn.access",)),
}


@dataclass
class LoggingConfig:
    """Resolved logging configuration (defaults ← ``config/logging.toml``)."""

    console_level: int = logging.INFO
    json_lines: bool = True
    module_levels: dict[str, int] = field(default_factory=dict)
    families: dict[str, FamilySpec] = field(default_factory=lambda: dict(DEFAULT_FAMILIES))
    sql_echo: bool = False

    @classmethod
    def load(cls, path: Path | None) -> LoggingConfig:
        """Read ``config/logging.toml`` if present; unknown keys are ignored, errors fall back to defaults."""
        cfg = cls()
        if path is None or not path.exists():
            return cfg
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logging.getLogger(ROOT_LOGGER).warning("logging.toml unreadable (%s) — defaults in use", exc)
            return cfg
        general = data.get("logging", {})
        cfg.console_level = _level(general.get("console_level", "INFO"))
        cfg.json_lines = bool(general.get("json_lines", True))
        cfg.sql_echo = bool(general.get("sql_echo", False))
        cfg.module_levels = {str(k): _level(v) for k, v in data.get("levels", {}).items()}
        for name, spec in data.get("families", {}).items():
            if name in cfg.families and isinstance(spec, dict):
                fam = cfg.families[name]
                fam.backup_days = int(spec.get("days", fam.backup_days))
                fam.level = _level(spec.get("level", logging.getLevelName(fam.level)))
        return cfg


def _level(value: Any) -> int:
    if isinstance(value, int):
        return value
    lvl = logging.getLevelName(str(value).upper())
    return lvl if isinstance(lvl, int) else logging.INFO


class _PrefixFilter(logging.Filter):
    def __init__(self, prefixes: tuple[str, ...]) -> None:
        super().__init__()
        self.prefixes = prefixes

    def filter(self, record: logging.LogRecord) -> bool:
        return not self.prefixes or any(record.name == p or record.name.startswith(p + ".") for p in self.prefixes)


@dataclass
class LoggingHandle:
    """What ``setup_logging`` returns; call ``shutdown()`` on exit to flush the queue."""

    logs_dir: Path
    listener: logging.handlers.QueueListener
    handlers: list[logging.Handler]
    config: LoggingConfig

    def shutdown(self) -> None:
        """Flush and close everything (safe to call twice)."""
        try:
            self.listener.stop()
        except (RuntimeError, AttributeError):
            pass
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.handlers.QueueHandler):
                root.removeHandler(h)
        for h in self.handlers:
            try:
                h.close()
            except OSError:
                pass


def setup_logging(logs_dir: Path, *, config_path: Path | None = None, console: bool = True, console_stream: Any = None) -> LoggingHandle:
    """Configure the root logger for the application. Idempotent: a previous setup is torn down."""
    cfg = LoggingConfig.load(config_path)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)

    handlers: list[logging.Handler] = []
    for name, fam in cfg.families.items():
        if fam.json_lines and not cfg.json_lines and name != "audit":
            continue
        folder = logs_dir / fam.folder
        folder.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(folder / fam.filename, when="midnight", backupCount=fam.backup_days, encoding="utf-8", utc=False)
        fh.setLevel(fam.level)
        fh.setFormatter(JsonFormatter() if fam.json_lines else KeyValueFormatter())
        if fam.logger_prefixes:
            fh.addFilter(_PrefixFilter(fam.logger_prefixes))
        handlers.append(fh)

    if console:
        ch = logging.StreamHandler(console_stream or sys.stderr)
        ch.setLevel(cfg.console_level)
        ch.setFormatter(KeyValueFormatter())
        handlers.append(ch)

    q: queue.Queue[logging.LogRecord] = queue.Queue(-1)
    qh = logging.handlers.QueueHandler(q)
    qh.addFilter(ContextFilter())  # runs on the caller's thread so ContextVars are read correctly
    root.addHandler(qh)
    listener = logging.handlers.QueueListener(q, *handlers, respect_handler_level=True)
    listener.start()

    for name, level in cfg.module_levels.items():
        logging.getLogger(name).setLevel(level)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO if cfg.sql_echo else logging.WARNING)
    for noisy in ("httpx", "httpcore", "urllib3", "PIL", "matplotlib", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(max(logging.getLogger(noisy).level, logging.WARNING))

    logging.getLogger(ROOT_LOGGER).info("LOGGING_READY", extra={"logs_dir": str(logs_dir), "families": sorted(cfg.families), "console_level": logging.getLevelName(cfg.console_level)})
    return LoggingHandle(logs_dir, listener, handlers, cfg)


def get_logger(name: str) -> logging.Logger:
    """``logging.getLogger`` with the convention that app loggers live under ``vaishnavi.``."""
    return logging.getLogger(name if name.startswith(ROOT_LOGGER) else f"{ROOT_LOGGER}.{name}")
