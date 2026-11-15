"""Install-log setup: file handler from the first millisecond, run id, secret redaction (stdlib only).

Install logs live in ``<program folder>/logs/install/`` because the owner's data location is not known
until the locations stage runs. Each run writes ``<YYYY-MM-DD_HHMMSS>_<run-id>.log`` at DEBUG level;
the console shows only plain-language lines (INFO+ when ``--debug``).
"""

from __future__ import annotations

import logging
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s run=%(run_id)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
KEEP_INSTALL_LOGS = 30

# Anything that looks like a credential is masked before it reaches any handler.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # key=value / key: value / key "value" forms; the value may carry a "Bearer " prefix
    re.compile(
        r"(?i)\b(api[_-]?key|token|password|passwd|pwd|secret|authorization|bearer)"
        r"(\s*[=:]\s*|\s+)(['\"]?)((?:bearer\s+)?[^'\"\s,;]+)\3"
    ),
    # OpenAI-style keys and Hugging Face tokens
    re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})"),
    re.compile(r"\b(hf_[A-Za-z0-9]{8,})"),
)
MASK = "***"


def redact(text: str) -> str:
    """Return ``text`` with credential-like values replaced by ``***``."""
    out = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    out = _SECRET_PATTERNS[1].sub(MASK, out)
    return _SECRET_PATTERNS[2].sub(MASK, out)


class RedactingFilter(logging.Filter):
    """Masks secrets in the rendered message and its arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except (TypeError, ValueError):
            rendered = str(record.msg)
        record.msg = redact(rendered)
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Redacts the *whole* formatted line — including tracebacks, which can quote an API key."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class RunIdFilter(logging.Filter):
    """Stamps every record with the installer run id."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def new_run_id() -> str:
    """Short, log-friendly id unique enough for one PC's install history."""
    return datetime.now().strftime("%y%m%d%H%M%S") + secrets.token_hex(2)


def setup_install_logging(log_dir: Path, run_id: str, *, debug_console: bool = False) -> Path:
    """Create ``log_dir``, attach the file (+ optional console) handlers, return the log path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = log_dir / f"{stamp}_{run_id}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = RedactingFormatter(LOG_FORMAT, DATE_FORMAT)
    run_filter = RunIdFilter(run_id)
    redacting = RedactingFilter()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(run_filter)
    file_handler.addFilter(redacting)
    root.addHandler(file_handler)

    if debug_console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(run_filter)
        console_handler.addFilter(redacting)
        root.addHandler(console_handler)

    prune_old_logs(log_dir, keep=KEEP_INSTALL_LOGS)
    logging.getLogger("bootstrap.logsetup").debug("install log opened: %s", log_path)
    return log_path


def prune_old_logs(log_dir: Path, *, keep: int) -> int:
    """Delete the oldest ``*.log`` files beyond ``keep``; returns how many were removed."""
    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    removed = 0
    for old in logs[: max(0, len(logs) - keep)]:
        try:
            old.unlink()
            removed += 1
        except OSError as exc:  # a locked file must never stop the installer
            logging.getLogger("bootstrap.logsetup").warning("could not prune %s: %s", old, exc)
    return removed
