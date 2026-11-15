"""Password / Owner-PIN hashing, lockout policy, recovery key and the encryption box (core).

Hashing is **stdlib ``hashlib.scrypt``** (RFC 7914, memory-hard) in a self-describing PHC-style
string ``$scrypt$ln=15,r=8,p=1$<salt>$<hash>`` so parameters can be raised later and old hashes
keep verifying (see ADR-011 note). Verification is constant-time. The Owner PIN (4–6 digits) gets the
same treatment plus a stricter lockout (docs/08: ``E-AUTH-030`` after 3 failures, 15 minutes).

Encryption of owner secrets (bank/UPI, API keys) uses ``cryptography``'s Fernet (AES-128-CBC +
HMAC) behind the :class:`SecretBox` protocol; the library is imported lazily so this module stays
importable — and its hashing part testable — without the venv. Never hand-roll crypto here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Protocol

from vaishnavi.core.clock import now_utc

SCRYPT_LN: Final = 15  # 2**15 = 32768 — ~100 ms on a modest CPU
SCRYPT_R: Final = 8
SCRYPT_P: Final = 1
SCRYPT_DKLEN: Final = 32
SALT_BYTES: Final = 16
MIN_PASSWORD_LEN: Final = 6
PIN_MIN_LEN, PIN_MAX_LEN = 4, 6
RECOVERY_WORDS: Final = 12


class SecurityError(ValueError):
    """Malformed hash string or unsupported algorithm (programming error, not a user error)."""


# ----------------------------------------------------------------------------- hashing
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(secret: str, salt: bytes, ln: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=2**ln, r=r, p=p, dklen=SCRYPT_DKLEN, maxmem=256 * 1024 * 1024)


def hash_secret(secret: str, *, ln: int = SCRYPT_LN, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    """Hash a password or PIN → ``$scrypt$ln=15,r=8,p=1$<salt>$<hash>`` (new random salt each call)."""
    if not isinstance(secret, str) or secret == "":
        raise SecurityError("secret must be a non-empty string")
    salt = _secrets.token_bytes(SALT_BYTES)
    return f"$scrypt$ln={ln},r={r},p={p}${_b64(salt)}${_b64(_scrypt(secret, salt, ln, r, p))}"


def verify_secret(secret: str, stored: str) -> bool:
    """Constant-time check of ``secret`` against a stored hash; False (never an exception) on mismatch.

    Accepts ``$scrypt$…`` (ours) and, if the ``argon2`` package happens to be installed, ``$argon2…``
    hashes from a possible future migration.
    """
    if not stored or not isinstance(secret, str):
        return False
    if stored.startswith("$scrypt$"):
        try:
            _, _, params, salt_b64, hash_b64 = stored.split("$")
            kv = dict(item.split("=") for item in params.split(","))
            expected = _unb64(hash_b64)
            actual = _scrypt(secret, _unb64(salt_b64), int(kv["ln"]), int(kv["r"]), int(kv["p"]))
        except (ValueError, KeyError, TypeError):
            return False
        return hmac.compare_digest(actual, expected)
    if stored.startswith("$argon2"):
        try:
            from argon2 import PasswordHasher  # noqa: PLC0415 (optional)
            from argon2.exceptions import VerificationError  # noqa: PLC0415

            return bool(PasswordHasher().verify(stored, secret))
        except ImportError:
            return False
        except VerificationError:
            return False
        except Exception:
            return False
    return False


def needs_rehash(stored: str, *, ln: int = SCRYPT_LN, r: int = SCRYPT_R, p: int = SCRYPT_P) -> bool:
    """True when the stored hash uses weaker parameters than the current defaults (rehash on next login)."""
    if not stored.startswith("$scrypt$"):
        return True
    try:
        params = dict(item.split("=") for item in stored.split("$")[2].split(","))
        return (int(params["ln"]), int(params["r"]), int(params["p"])) != (ln, r, p)
    except (ValueError, KeyError, IndexError):
        return True


def password_problem(password: str) -> str | None:
    """Plain reason why a new password is unacceptable, or None (6+ chars; digits-only allowed for staff speed)."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"at least {MIN_PASSWORD_LEN} characters"
    if password.strip() != password:
        return "no spaces at the start or end"
    if password.lower() in _COMMON_PASSWORDS:
        return "too common"
    return None


def pin_problem(pin: str) -> str | None:
    """Plain reason why an Owner PIN is unacceptable, or None (4–6 digits, not trivial)."""
    if not pin.isdigit() or not PIN_MIN_LEN <= len(pin) <= PIN_MAX_LEN:
        return f"{PIN_MIN_LEN} to {PIN_MAX_LEN} digits"
    if len(set(pin)) == 1 or pin in _TRIVIAL_PINS:
        return "too easy to guess"
    return None


_COMMON_PASSWORDS: Final = frozenset({"123456", "password", "12345678", "qwerty", "111111", "123123", "abc123", "vaishnavi", "puja123", "shop123"})
_TRIVIAL_PINS: Final = frozenset({"1234", "12345", "123456", "0000", "4321", "654321", "1111", "2580", "0852", "1122"})


# ----------------------------------------------------------------------------- lockout
@dataclass
class LockoutPolicy:
    """Count failures per subject; lock after ``max_failures`` for ``lock_minutes`` (E-AUTH-030)."""

    max_failures: int = 3
    lock_minutes: int = 15
    _failures: dict[str, int] = field(default_factory=dict, repr=False)
    _locked_until: dict[str, datetime] = field(default_factory=dict, repr=False)

    def is_locked(self, subject: str) -> bool:
        """True while the subject is locked (expired locks are cleared)."""
        until = self._locked_until.get(subject)
        if until is None:
            return False
        if now_utc() >= until:
            self._locked_until.pop(subject, None)
            self._failures.pop(subject, None)
            return False
        return True

    def seconds_remaining(self, subject: str) -> int:
        """Seconds until the lock expires (0 when not locked)."""
        until = self._locked_until.get(subject)
        return max(0, int((until - now_utc()).total_seconds())) if until and self.is_locked(subject) else 0

    def record_failure(self, subject: str) -> bool:
        """Register a wrong attempt; returns True when this one triggered the lock."""
        count = self._failures.get(subject, 0) + 1
        self._failures[subject] = count
        if count >= self.max_failures:
            self._locked_until[subject] = now_utc() + timedelta(minutes=self.lock_minutes)
            return True
        return False

    def record_success(self, subject: str) -> None:
        """Clear the counter after a correct attempt."""
        self._failures.pop(subject, None)
        self._locked_until.pop(subject, None)

    def failures(self, subject: str) -> int:
        """Current failure count."""
        return self._failures.get(subject, 0)


# ----------------------------------------------------------------------------- recovery key
def generate_recovery_key(wordlist: tuple[str, ...]) -> str:
    """12 words from ``wordlist`` (≥ 2048 entries → ≥ 132 bits), space-separated, lower-case."""
    if len(wordlist) < 2048:
        raise SecurityError("wordlist too small for a safe recovery key")
    return " ".join(_secrets.choice(wordlist) for _ in range(RECOVERY_WORDS))


def normalise_recovery_key(text: str) -> str:
    """Owner-typed key → canonical form (lower-case, single spaces) so typing style does not matter."""
    return " ".join(text.lower().split())


# ----------------------------------------------------------------------------- encryption box
class SecretBox(Protocol):
    """Authenticated encryption for ``secure_setting`` values."""

    def encrypt(self, plaintext: str) -> str:
        """→ opaque token (text)."""
        ...

    def decrypt(self, token: str) -> str:
        """Inverse; raises ``ValueError`` when the token is corrupt or the key is wrong."""
        ...


def new_master_key() -> str:
    """A fresh Fernet key (URL-safe base64, 32 random bytes) — stored in the OS keyring."""
    return _b64(_secrets.token_bytes(32)) + "="  # Fernet wants standard 44-char padding


class FernetBox:
    """``SecretBox`` on ``cryptography.fernet`` (imported lazily)."""

    def __init__(self, master_key: str) -> None:
        try:
            from cryptography.fernet import Fernet, InvalidToken  # noqa: PLC0415
        except ImportError as exc:  # the venv is missing the package — a doctor FAIL, not a crash here
            raise RuntimeError("cryptography is not installed; secure settings are unavailable") from exc
        self._invalid = InvalidToken
        self._fernet = Fernet(master_key.encode("ascii"))

    def encrypt(self, plaintext: str) -> str:
        """Encrypt + authenticate."""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Verify + decrypt; ``ValueError`` on tampering or wrong key."""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except self._invalid as exc:
            raise ValueError("secure value is corrupt or was encrypted with another key") from exc


class NullBox:
    """Refuses to store secrets — used when no key store exists so nothing is silently written in clear."""

    def encrypt(self, plaintext: str) -> str:
        """Always fails."""
        raise RuntimeError("no secret store available")

    def decrypt(self, token: str) -> str:
        """Always fails."""
        raise RuntimeError("no secret store available")


def masked_preview(value: str, keep: int = 4) -> str:
    """'•••• 1234' — what the UI/log may show of a secret value (``keep=0`` or a short value → '••••')."""
    tail = value[-keep:] if 0 < keep < len(value) else ""
    return f"•••• {tail}".strip()
