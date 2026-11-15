"""Mini log console: a stdlib HTTP server that tails the install logs in any browser (port 8081).

Started as the very first stage so that installation and start-up problems are visible from a phone
on the shop Wi-Fi *before* any third-party package exists. Serves only ``*.log`` files and
``state.json`` from the install-log folder; secrets are already masked when written. Once the
application is up, its own ``/system/logs`` console takes over and this server is stopped.
"""

from __future__ import annotations

import html
import json
import logging
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from bootstrap.stages import ExitCode, StageContext, StageResult

if TYPE_CHECKING:
    from bootstrap.cli import Options

log = logging.getLogger("bootstrap.minilog")

DEFAULT_PORT = 8081
PORT_ATTEMPTS = 5
DEFAULT_LINES = 200
MAX_LINES = 2000
REFRESH_SECONDS = 3
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
RUNTIME_KEY = "minilog_server"


def local_ip() -> str:
    """Best-effort LAN address of this PC (no packet is sent); falls back to 127.0.0.1."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def tail(path: Path, lines: int) -> list[str]:
    """Last ``lines`` lines of a text file (UTF-8, errors replaced)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return list(deque(fh, maxlen=lines))
    except OSError as exc:
        log.warning("minilog: cannot read %s: %s", path, exc)
        return []


def level_of(line: str) -> str:
    """Log level of an install-log line ('' when the line has none, e.g. a traceback)."""
    parts = line.split()
    if len(parts) >= 3 and parts[2] in LEVELS:
        return parts[2]
    return ""


def filter_lines(lines: list[str], min_level: str) -> list[str]:
    """Keep lines at or above ``min_level``; continuation lines follow their parent's fate."""
    if min_level not in LEVELS or min_level == "DEBUG":
        return lines
    threshold = LEVELS.index(min_level)
    keep: list[str] = []
    keeping = False
    for line in lines:
        lvl = level_of(line)
        if lvl:
            keeping = LEVELS.index(lvl) >= threshold
        if keeping:
            keep.append(line)
    return keep


class _LogDirectory:
    """Read-only, sandboxed view of the install-log folder."""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir.resolve()

    def files(self) -> list[Path]:
        if not self.log_dir.exists():
            return []
        return sorted(self.log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)

    def newest(self) -> Path | None:
        files = self.files()
        return files[0] if files else None

    def resolve(self, name: str | None) -> Path | None:
        """Map a requested file name to a path inside the folder, or None when not allowed."""
        if name is None:
            return self.newest()
        safe = Path(name).name
        if safe != name or not (safe.endswith(".log") or safe == "state.json"):
            return None
        candidate = (self.log_dir / safe).resolve()
        if candidate.parent != self.log_dir or not candidate.is_file():
            return None
        return candidate


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}">
<title>{title}</title>
<style>
body{{margin:0;background:#111;color:#ddd;font:13px/1.45 ui-monospace,Consolas,monospace}}
header{{position:sticky;top:0;background:#e07b00;color:#fff;padding:8px 14px;font:600 15px system-ui,sans-serif}}
header small{{font-weight:400;opacity:.9;margin-left:12px}}
nav{{padding:6px 14px;background:#1b1b1b;border-bottom:1px solid #333;font:12px system-ui,sans-serif}}
nav a{{color:#ffb866;margin-right:12px;text-decoration:none}} nav a.on{{text-decoration:underline}}
pre{{margin:0;padding:10px 14px;white-space:pre-wrap;word-break:break-all}}
.WARNING{{color:#ffcf5a}} .ERROR,.CRITICAL{{color:#ff6b6b;font-weight:600}} .DEBUG{{color:#8a8a8a}} .INFO{{color:#ddd}}
footer{{padding:8px 14px;color:#777;font:11px system-ui,sans-serif}}
</style></head><body>
<header>{title}<small>{file}</small><small>{updated}</small></header>
<nav>Level: {level_links} &nbsp;|&nbsp; Files: {file_links} &nbsp;|&nbsp; <a href="/raw{raw_qs}">raw</a> <a href="/state">state</a></nav>
<pre>{body}</pre>
<footer>Refreshes every {refresh} s · showing last {shown} of up to {lines} lines · Vaishnavi Store Manager install console</footer>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    """Routes: / (HTML), /raw, /files, /state, /healthz."""

    server: MiniLogServer  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: object) -> None:  # route access logs into our logger
        log.debug("minilog: %s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                self._page(qs)
            elif url.path == "/raw":
                self._raw(qs)
            elif url.path == "/files":
                self._json({"files": [p.name for p in self.server.logs.files()]})
            elif url.path == "/state":
                self._state()
            elif url.path == "/healthz":
                newest = self.server.logs.newest()
                self._json({"status": "ok", "file": newest.name if newest else None, "uptime_s": int(time.time() - self.server.started_at)})
            else:
                self._text("not found", HTTPStatus.NOT_FOUND)
        except Exception as exc:  # the console must never take the installer down
            log.exception("minilog: request failed: %r", exc)
            self._text("internal error", HTTPStatus.INTERNAL_SERVER_ERROR)

    # -- responses ------------------------------------------------------------------------
    def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(text.encode("utf-8"), "text/plain; charset=utf-8", status)

    def _json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def _lines_param(self, qs: dict[str, str]) -> int:
        try:
            return max(1, min(MAX_LINES, int(qs.get("lines", DEFAULT_LINES))))
        except ValueError:
            return DEFAULT_LINES

    def _raw(self, qs: dict[str, str]) -> None:
        path = self.server.logs.resolve(qs.get("file"))
        if path is None:
            self._text("no log file", HTTPStatus.NOT_FOUND)
            return
        lines = filter_lines(tail(path, self._lines_param(qs)), qs.get("level", "DEBUG").upper())
        self._text("".join(lines))

    def _state(self) -> None:
        path = self.server.logs.resolve("state.json")
        if path is None:
            self._json({"error": "no state yet"}, HTTPStatus.NOT_FOUND)
            return
        self._send(path.read_bytes(), "application/json; charset=utf-8")

    def _page(self, qs: dict[str, str]) -> None:
        logs = self.server.logs
        path = logs.resolve(qs.get("file"))
        level = qs.get("level", "DEBUG").upper()
        if level not in LEVELS:
            level = "DEBUG"
        limit = self._lines_param(qs)
        lines = filter_lines(tail(path, limit), level) if path else []
        body = "".join(f'<span class="{html.escape(level_of(l) or "cont")}">{html.escape(l)}</span>' for l in lines) or (
            "(waiting for the first log line …)" if path is None else "(no lines at this level)"
        )
        file_qs = f"&file={path.name}" if path and qs.get("file") else ""
        level_links = " ".join(
            f'<a class="{"on" if lv == level else ""}" href="/?level={lv}{file_qs}">{lv}</a>' for lv in LEVELS
        )
        file_links = " ".join(
            f'<a class="{"on" if path and p.name == path.name else ""}" href="/?file={p.name}&level={level}">{html.escape(p.name)}</a>'
            for p in logs.files()[:8]
        ) or "–"
        page = _PAGE.format(
            refresh=REFRESH_SECONDS,
            title=html.escape(self.server.title),
            file=html.escape(path.name) if path else "",
            updated=time.strftime("%H:%M:%S"),
            level_links=level_links,
            file_links=file_links,
            raw_qs=f"?level={level}{file_qs}",
            body=body,
            shown=len(lines),
            lines=limit,
        )
        self._send(page.encode("utf-8"), "text/html; charset=utf-8")


class MiniLogServer(ThreadingHTTPServer):
    """Threaded HTTP server tailing the install logs; runs in a daemon thread."""

    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second socket bind an already-used port, which would hide a
    # port conflict instead of triggering the fallback; POSIX only avoids TIME_WAIT delays with it.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, log_dir: Path, host: str = "0.0.0.0", port: int = DEFAULT_PORT, title: str = "Vaishnavi Store Manager — install log") -> None:  # noqa: S104 (LAN access is the purpose)
        self.logs = _LogDirectory(log_dir)
        self.title = title
        self.started_at = time.time()
        self._thread: threading.Thread | None = None
        super().__init__((host, port), _Handler)

    @classmethod
    def start(cls, log_dir: Path, host: str = "0.0.0.0", port: int = DEFAULT_PORT, attempts: int = PORT_ATTEMPTS) -> MiniLogServer:  # noqa: S104
        """Bind to ``port`` or the next free one (``port+1 …``), start serving in the background."""
        last_error: OSError | None = None
        candidates = [port] if port == 0 else [port + i for i in range(attempts)]
        for candidate in candidates:
            try:
                server = cls(log_dir, host, candidate)
            except OSError as exc:
                last_error = exc
                log.debug("minilog: port %d busy (%s), trying next", candidate, exc)
                continue
            server._thread = threading.Thread(target=server.serve_forever, name="minilog", daemon=True)
            server._thread.start()
            log.info("MINILOG_STARTED url=%s dir=%s", server.url(), log_dir)
            return server
        raise OSError(f"no free port in {candidates[0]}–{candidates[-1]}: {last_error}")

    @property
    def port(self) -> int:
        """Actual bound port."""
        return int(self.server_address[1])

    def url(self, host: str | None = None) -> str:
        """Browsable URL; uses the LAN IP so a phone on the shop Wi-Fi can open it."""
        return f"http://{host or local_ip()}:{self.port}/"

    def stop(self) -> None:
        """Stop serving and release the port."""
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        log.info("MINILOG_STOPPED port=%d", self.port)


# ----------------------------------------------------------------------------- stage + mode
@dataclass
class LogConsoleStage:
    """Stage 0: start the mini log console so the owner can watch everything that follows."""

    key: str = "log_console"
    title_key: str = "stage.log_console"
    port: int = DEFAULT_PORT

    def applies(self, ctx: StageContext) -> bool:
        """Always; it is the owner's window into the installer."""
        return True

    def cache_key(self, ctx: StageContext) -> str | None:
        """Never cached — a server must be started on every run."""
        return None

    def run(self, ctx: StageContext) -> StageResult:
        """Start the server; a busy port range is a WARN (installation continues without the console)."""
        try:
            server = MiniLogServer.start(ctx.paths.install_log_dir, port=self.port)
        except OSError as exc:
            return StageResult.warn(ctx.t("stage.log_console.unavailable"), error_code="E-SYS-007", detail=str(exc), hint=ctx.t("stage.log_console.hint"))
        ctx.runtime[RUNTIME_KEY] = server
        url = server.url()
        return StageResult.ok(ctx.t("stage.log_console.started", url=url), url=url, port=server.port)


def stop_log_console(ctx: StageContext) -> None:
    """Hand-over helper for the start stage: stop the mini console once the app's own console is up."""
    server = ctx.runtime.pop(RUNTIME_KEY, None)
    if isinstance(server, MiniLogServer):
        server.stop()


def run_logs_mode(ctx: StageContext) -> ExitCode:
    """``main.py --logs``: serve the console until Ctrl-C."""
    try:
        server = MiniLogServer.start(ctx.paths.install_log_dir)
    except OSError as exc:
        ctx.console.fail(ctx.t("stage.log_console.unavailable") + f"  (E-SYS-007: {exc})")
        return ExitCode.FAIL
    ctx.console.ok(ctx.t("mode.logs.running", url=server.url()))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
        ctx.console.line()
        ctx.console.info(ctx.t("mode.logs.stopped"))
        return ExitCode.OK


def register(options: Options) -> LogConsoleStage:
    """Factory used by ``build_default_pipeline``."""
    return LogConsoleStage()
