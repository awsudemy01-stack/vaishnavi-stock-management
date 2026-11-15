"""Where the master encryption key lives: the OS key store, or a protected file as fallback.

Only the *master key* goes here (one 44-character string). Secrets themselves are encrypted with
it inside the database (``secure_setting``). Backends, in order of preference:

1. **OS keyring** via the ``keyring`` package — Windows Credential Manager (DPAPI), macOS Keychain,
   Linux SecretService/KWallet. Imported lazily; absence or a broken backend is not fatal.
2. **File** ``<primary>/.keys/master.key`` with mode 0600 (and hidden on Windows) — reported as
   ``E-SEC-001`` so the owner knows protection is weaker than it could be.

This is the only module that touches the keyring library (import-safety rule, docs/10 §6).
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from vaishnavi.core.security import new_master_key

log = logging.getLogger("vaishnavi.platform.secrets")

SERVICE: Final = "vaishnavi-store-manager"
ACCOUNT: Final = "master-key"
KEY_DIR: Final = ".keys"
KEY_FILE: Final = "master.key"


@dataclass(frozen=True)
class MasterKey:
    """The key plus where it came from."""

    value: str
    backend: str  # keyring | file
    created: bool  # True when generated in this call


class KeyStoreUnavailable(RuntimeError):
    """Neither the keyring nor the file fallback could be used (``E-SEC-001`` escalates to FAIL)."""


# ----------------------------------------------------------------------------- keyring backend
def _keyring_get() -> str | None:
    try:
        import keyring  # noqa: PLC0415
        from keyring.errors import KeyringError  # noqa: PLC0415
    except ImportError:
        log.debug("keyring package not installed")
        return None
    try:
        return keyring.get_password(SERVICE, ACCOUNT)
    except KeyringError as exc:
        log.warning("keyring backend failed on read: %s", exc)
        return None
    except Exception as exc:  # some backends raise odd exceptions when no session bus exists
        log.warning("keyring backend unusable: %r", exc)
        return None


def _keyring_set(value: str) -> bool:
    try:
        import keyring  # noqa: PLC0415
        from keyring.errors import KeyringError  # noqa: PLC0415
    except ImportError:
        return False
    try:
        keyring.set_password(SERVICE, ACCOUNT, value)
        return keyring.get_password(SERVICE, ACCOUNT) == value
    except KeyringError as exc:
        log.warning("keyring backend failed on write: %s", exc)
        return False
    except Exception as exc:
        log.warning("keyring backend unusable on write: %r", exc)
        return False


def keyring_available() -> bool:
    """True when the keyring package imports and a backend answers (cheap probe)."""
    try:
        import keyring  # noqa: PLC0415
        from keyring.backends.fail import Keyring as FailKeyring  # noqa: PLC0415
    except ImportError:
        return False
    try:
        return not isinstance(keyring.get_keyring(), FailKeyring)
    except Exception:
        return False


# ----------------------------------------------------------------------------- file backend
def key_file_path(primary: Path) -> Path:
    """``<primary>/.keys/master.key``."""
    return primary / KEY_DIR / KEY_FILE


def _harden(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        log.debug("chmod on key file failed: %s", exc)
    if sys.platform.startswith("win"):
        try:
            import ctypes  # noqa: PLC0415

            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)  # type: ignore[attr-defined]  # FILE_ATTRIBUTE_HIDDEN
        except Exception as exc:
            log.debug("hiding key file failed: %r", exc)


def _file_get(primary: Path) -> str | None:
    path = key_file_path(primary)
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("master key file unreadable: %s", exc)
        return None
    return value or None


def _file_set(primary: Path, value: str) -> bool:
    path = key_file_path(primary)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not sys.platform.startswith("win"):
            _harden(path.parent)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(value + "\n", encoding="ascii")
        _harden(tmp)
        os.replace(tmp, path)
        _harden(path)
        return True
    except OSError as exc:
        log.error("cannot write master key file %s: %s", path, exc)
        return False


# ----------------------------------------------------------------------------- public
def load_or_create_master_key(primary: Path, *, allow_file_fallback: bool = True, prefer_keyring: bool = True) -> MasterKey:
    """Return the master key, creating one on first run. Never logs the key.

    Order: keyring (read) → file (read) → create → keyring (write) → file (write).
    A key found in the file while the keyring now works is **migrated** into the keyring and the
    file is removed, so protection improves automatically once a key store becomes available.
    """
    if prefer_keyring:
        value = _keyring_get()
        if value:
            log.info("MASTER_KEY_LOADED backend=keyring")
            return MasterKey(value, "keyring", created=False)
    file_value = _file_get(primary) if allow_file_fallback else None
    if file_value:
        if prefer_keyring and _keyring_set(file_value):
            try:
                key_file_path(primary).unlink()
                log.info("MASTER_KEY_MIGRATED file->keyring")
            except OSError as exc:
                log.warning("key migrated to keyring but file could not be removed: %s", exc)
            return MasterKey(file_value, "keyring", created=False)
        log.warning("MASTER_KEY_LOADED backend=file code=E-SEC-001")
        return MasterKey(file_value, "file", created=False)

    value = new_master_key()
    if prefer_keyring and _keyring_set(value):
        log.info("MASTER_KEY_CREATED backend=keyring")
        return MasterKey(value, "keyring", created=True)
    if allow_file_fallback and _file_set(primary, value):
        log.warning("MASTER_KEY_CREATED backend=file code=E-SEC-001")
        return MasterKey(value, "file", created=True)
    raise KeyStoreUnavailable("no keyring backend and the key file could not be written")


def forget_master_key(primary: Path) -> None:
    """Remove the key from every backend (used only by 'set up a new shop' — data becomes unreadable)."""
    try:
        import keyring  # noqa: PLC0415

        keyring.delete_password(SERVICE, ACCOUNT)
    except Exception as exc:
        log.debug("keyring delete: %r", exc)
    try:
        key_file_path(primary).unlink(missing_ok=True)
    except OSError as exc:
        log.debug("key file delete: %s", exc)
    log.warning("MASTER_KEY_FORGOTTEN")
