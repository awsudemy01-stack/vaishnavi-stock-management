"""Doctor check: ports free (unless the running app asks) and the clock is not behind the last recorded event (stdlib)."""

from __future__ import annotations

import socket
import sqlite3
from dataclasses import dataclass

from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext
from vaishnavi.core.clock import now_utc, parse_iso_utc

CLOCK_TOLERANCE_MIN = 5


def port_free(host: str, port: int) -> bool:
    """True when nothing listens on ``host:port`` (connect attempt refused)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
        except OSError:
            return True
        return False


def last_event_at(ctx: DoctorContext) -> str | None:
    """Newest timestamp the database knows (sale / audit / identity) — the clock must not be before it."""
    explicit = ctx.runtime.get("last_event_at")
    if explicit:
        return str(explicit)
    db = ctx.paths.database
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            candidates = []
            for sql in ("SELECT MAX(sold_at) FROM sale", "SELECT MAX(at) FROM audit_log", "SELECT MAX(COALESCE(last_clean_close_at, last_open_at)) FROM db_identity"):
                try:
                    row = conn.execute(sql).fetchone()
                    if row and row[0]:
                        candidates.append(str(row[0]))
                except sqlite3.DatabaseError:
                    continue
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None
    return max(candidates) if candidates else None


@dataclass
class NetworkCheck:
    """FAIL when the web port is taken by another program; WARN E-SYS-030 when the clock runs behind the data."""

    key: str = "network"
    group: str = "system"

    def run(self, ctx: DoctorContext) -> CheckResult:
        """Ports first (hard), then the clock (soft)."""
        host = ctx.settings.server.host or "127.0.0.1"
        ports = [ctx.settings.server.port, ctx.settings.server.https_port]
        if not ctx.runtime.get("app_running"):
            for port in ports:
                if not port_free(host, port):
                    return CheckResult.fail(ctx.t("doctor.network.port_busy", port=port), error_code="E-SYS-017", hint=ctx.t("doctor.network.port_busy_hint"), detail=f"{host}:{port} in use", port=port)
        last = last_event_at(ctx)
        if last:
            try:
                behind = (parse_iso_utc(last) - now_utc()).total_seconds() / 60
            except ValueError:
                behind = 0
            if behind > CLOCK_TOLERANCE_MIN:
                return CheckResult.warn(ctx.t("doctor.network.clock_behind", minutes=int(behind)), error_code="E-SYS-030", hint=ctx.t("doctor.network.clock_hint"), detail=f"last_event={last}", minutes=int(behind))
        return CheckResult.ok(ctx.t("doctor.network.ok", ports="/".join(str(p) for p in ports)), ports=ports, last_event_at=last)
