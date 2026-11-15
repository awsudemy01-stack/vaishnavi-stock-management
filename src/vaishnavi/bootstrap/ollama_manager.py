"""OllamaManager — locate, start, verify and prepare the local AI engine every morning (stdlib ``urllib``; docs/07 §5 A2–A6).

Used by the doctor (and its ``--fix``), by *Settings → Assistant* and by the start-up orchestrator:
``is_running()`` → ``start()`` (detached ``ollama serve``, waits for the port) → ``missing_models()`` →
``ensure_models()`` (``/api/pull`` streamed with progress) → ``warm_up()`` (one tiny generate so the
first question of the day is not slow). Every network call has a timeout and never raises for
"not running"; failures are logged with the ``E-AI-00x`` code the doctor will report.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

log = logging.getLogger("vaishnavi.bootstrap.ollama_manager")

DEFAULT_HOST = "http://127.0.0.1:11434"
START_WAIT_S = 25.0


def find_binary() -> str | None:
    """``ollama`` on PATH or in its default install locations."""
    found = shutil.which("ollama")
    if found:
        return found
    candidates: list[Path] = []
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        candidates += [Path(local) / "Programs" / "Ollama" / "ollama.exe", Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe"]
    elif sys.platform == "darwin":
        candidates += [Path("/usr/local/bin/ollama"), Path("/opt/homebrew/bin/ollama"), Path("/Applications/Ollama.app/Contents/Resources/ollama")]
    else:
        candidates += [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


class OllamaManager:
    """Thin client around the Ollama HTTP API plus process start."""

    def __init__(self, host: str = DEFAULT_HOST, *, binary: str | None = "auto", timeout: float = 5.0) -> None:
        self.host = host.rstrip("/")
        self.binary = find_binary() if binary == "auto" else binary
        self.timeout = timeout

    # -- http -------------------------------------------------------------------------------
    def _request(self, path: str, payload: dict | None = None, *, timeout: float | None = None) -> urllib.request.Request:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.host + path, data=data, method="POST" if data is not None else "GET")
        req.add_header("Content-Type", "application/json")
        return req

    def _json(self, path: str, payload: dict | None = None, *, timeout: float | None = None) -> dict | None:
        try:
            with urllib.request.urlopen(self._request(path, payload), timeout=timeout or self.timeout) as resp:  # noqa: S310 (local service)
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            log.debug("ollama %s failed: HTTP %s", path, exc.code)
            exc.close()
            return None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.debug("ollama %s failed: %s", path, exc)
            return None

    # -- service ------------------------------------------------------------------------------
    def is_running(self) -> bool:
        """True when ``/api/tags`` answers."""
        return self._json("/api/tags", timeout=min(self.timeout, 3.0)) is not None

    def start(self, *, wait_seconds: float = START_WAIT_S) -> bool:
        """Launch ``ollama serve`` detached and wait for the port; False when no binary or no answer."""
        if self.is_running():
            return True
        if not self.binary:
            log.warning("OLLAMA_START_SKIPPED reason=no-binary code=E-AI-002")
            return False
        creation = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)} if sys.platform.startswith("win") else {"start_new_session": True}
        try:
            proc = subprocess.Popen([self.binary, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **creation)  # noqa: S603
            log.info("OLLAMA_STARTING pid=%s binary=%s", getattr(proc, "pid", "?"), self.binary)
        except OSError as exc:
            log.error("OLLAMA_START_FAILED %s code=E-AI-001", exc)
            return False
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self.is_running():
                log.info("OLLAMA_READY host=%s", self.host)
                return True
            time.sleep(0.25)
        log.error("OLLAMA_NOT_READY after %.0fs code=E-AI-001", wait_seconds)
        return False

    # -- models -------------------------------------------------------------------------------
    def list_models(self) -> list[str]:
        """Installed model tags (``[]`` when not running)."""
        data = self._json("/api/tags")
        if not data:
            return []
        return [str(m.get("name", "")) for m in data.get("models", []) if isinstance(m, dict)]

    def has_model(self, name: str) -> bool:
        """``qwen2.5:3b`` matches ``qwen2.5:3b`` and ``qwen2.5:3b-instruct``; ``llama3`` matches ``llama3:latest``."""
        installed = self.list_models()
        base = name.split(":")[0]
        for tag in installed:
            if tag == name or tag.startswith(name) or (":" not in name and tag.split(":")[0] == base):
                return True
        return False

    def missing_models(self, names: Sequence[str]) -> list[str]:
        """Which of ``names`` are not installed."""
        return [n for n in names if not self.has_model(n)]

    def pull(self, name: str, *, on_progress: Callable[[str, int], None] | None = None, timeout: float = 3600) -> bool:
        """Stream ``/api/pull``; progress callback gets (status, percent)."""
        log.info("OLLAMA_PULL_START model=%s", name)
        try:
            with urllib.request.urlopen(self._request("/api/pull", {"name": name, "stream": True}), timeout=timeout) as resp:  # noqa: S310
                last_pct = -1
                for raw in resp:
                    try:
                        line = json.loads(raw.decode("utf-8").strip() or "{}")
                    except ValueError:
                        continue
                    status = str(line.get("status", ""))
                    total, done = line.get("total"), line.get("completed")
                    pct = int(done * 100 / total) if total and done is not None else (100 if status == "success" else last_pct if last_pct >= 0 else 0)
                    if on_progress and (pct != last_pct or status == "success"):
                        on_progress(status, pct)
                    last_pct = pct
                    if "error" in line:
                        log.error("OLLAMA_PULL_FAILED model=%s error=%s code=E-AI-004", name, line["error"])
                        return False
        except (urllib.error.URLError, OSError) as exc:
            log.error("OLLAMA_PULL_FAILED model=%s error=%s code=E-AI-004", name, exc)
            return False
        log.info("OLLAMA_PULL_DONE model=%s", name)
        return True

    def ensure_models(self, names: Sequence[str], *, on_progress: Callable[[str, str, int], None] | None = None) -> list[str]:
        """Pull whatever is missing; returns the names that were pulled successfully."""
        pulled: list[str] = []
        for name in self.missing_models(names):
            if self.pull(name, on_progress=(lambda s, p, _n=name: on_progress(_n, s, p)) if on_progress else None):
                pulled.append(name)
        return pulled

    def warm_up(self, model: str, *, keep_alive: str = "30m") -> int | None:
        """One empty generate so the model is resident; returns ms or None on failure."""
        started = time.perf_counter()
        data = self._json("/api/generate", {"model": model, "prompt": "", "keep_alive": keep_alive, "stream": False}, timeout=120)
        if data is None:
            log.warning("OLLAMA_WARMUP_FAILED model=%s code=E-AI-001", model)
            return None
        ms = int((time.perf_counter() - started) * 1000)
        log.info("OLLAMA_WARMUP_OK model=%s ms=%d", model, ms)
        return ms

    def embed_probe(self, model: str) -> bool:
        """One tiny embedding — proves the embedding model works."""
        data = self._json("/api/embeddings", {"model": model, "prompt": "namaste"}, timeout=60)
        return bool(data and data.get("embedding"))
