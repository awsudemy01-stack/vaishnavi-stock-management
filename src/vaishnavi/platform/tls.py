"""Self-signed TLS material for the optional HTTPS listener (``server.https_port``, docs/03 §6).

Uses ``cryptography`` (already a dependency for Fernet) when importable; otherwise returns None and the
server stays on plain HTTP on the LAN — the caller logs ``E-SYS-003`` style advice. The certificate covers
the hostnames/IPs given (shop PC name, 127.0.0.1, LAN IP) for 10 years and is reused once created.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
from collections.abc import Sequence
from pathlib import Path

log = logging.getLogger("vaishnavi.platform.tls")

CERT_NAME = "vaishnavi-cert.pem"
KEY_NAME = "vaishnavi-key.pem"


def ensure_self_signed(cert_dir: Path, *, hosts: Sequence[str], common_name: str = "Vaishnavi Store Manager") -> tuple[Path, Path] | None:
    """Return ``(cert_path, key_path)``, creating them once; None when ``cryptography`` is unavailable."""
    cert_dir = Path(cert_dir)
    cert_path, key_path = cert_dir / CERT_NAME, cert_dir / KEY_NAME
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    try:
        from cryptography import x509  # noqa: PLC0415
        from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415
        from cryptography.x509.oid import NameOID  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - optional dependency
        log.warning("TLS_UNAVAILABLE cryptography not importable (%s) — HTTPS listener disabled", exc)
        return None
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names: list[x509.GeneralName] = []
    for host in hosts:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_dir.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    try:
        key_path.chmod(0o600)
    except OSError:  # Windows
        pass
    log.info("TLS_SELF_SIGNED_CREATED cert=%s hosts=%s valid_days=3650", cert_path, ",".join(hosts))
    return cert_path, key_path
