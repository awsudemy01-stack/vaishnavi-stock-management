"""Ollama (local AI engine): install, start, pull models — stdlib only.

Used only when the owner chose the local provider. Everything talks to Ollama's HTTP API on
``http://127.0.0.1:11434`` with ``urllib``; the binary is located with ``shutil.which`` plus the
well-known install locations. Installation goes through the OS package manager (winget / brew) or
the official Linux script, elevated when required. Model pulls stream progress to the console.
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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bootstrap import elevate

log = logging.getLogger("bootstrap.ollama")

DEFAULT_HOST = "http://127.0.0.1:11434"
START_TIMEOUT_S = 45
PULL_TIMEOUT_S = 60 * 60  # a 2 GB model on slow shop internet
HTTP_TIMEOUT_S = 10

CHAT_MODELS = {"1.5b": "qwen2.5:1.5b-instruct", "3b": "qwen2.5:3b-instruct", "7b": "qwen2.5:7b-instruct"}
EMBED_MODELS = {"1.5b": "nomic-embed-text", "3b": "nomic-embed-text", "7b": "bge-m3"}

WELL_KNOWN_BINARIES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
    Path("/usr/local/bin/ollama"),
    Path("/opt/homebrew/bin/ollama"),
    Path("/usr/bin/ollama"),
    Path("/Applications/Ollama.app/Contents/Resources/ollama"),
)


def models_for_tier(tier: str) -> tuple[str, str]:
    """(chat_model, embed_model) for a hardware tier from the survey."""
    return CHAT_MODELS.get(tier, CHAT_MODELS["3b"]), EMBED_MODELS.get(tier, EMBED_MODELS["3b"])


def find_binary() -> Path | None:
    """Path to the ollama executable, or None."""
    found = shutil.which("ollama")
    if found:
        return Path(found)
    for candidate in WELL_KNOWN_BINARIES:
        if str(candidate) and candidate.is_file():
            return candidate
    return None


# ----------------------------------------------------------------------------- HTTP helpers
def _get_json(url: str, timeout: float = HTTP_TIMEOUT_S) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (localhost)
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("ollama GET %s failed: %s", url, exc)
        return None


def is_running(host: str = DEFAULT_HOST) -> bool:
    """Service answers on ``/api/tags``."""
    return _get_json(f"{host}/api/tags", timeout=3) is not None


def installed_models(host: str = DEFAULT_HOST) -> list[str]:
    """Names of locally available models (``name:tag``)."""
    data = _get_json(f"{host}/api/tags") or {}
    return [str(m.get("name", "")) for m in data.get("models", []) if isinstance(m, dict)]


def has_model(name: str, host: str = DEFAULT_HOST) -> bool:
    """True when ``name`` (with or without ``:latest``) is present."""
    names = set(installed_models(host))
    return name in names or f"{name}:latest" in names or (name.endswith(":latest") and name[:-7] in names)


def version(host: str = DEFAULT_HOST) -> str:
    """Server version string ('' if unknown)."""
    data = _get_json(f"{host}/api/version") or {}
    return str(data.get("version", ""))


# ----------------------------------------------------------------------------- start
def start_service(binary: Path, host: str = DEFAULT_HOST, timeout: float = START_TIMEOUT_S) -> bool:
    """Start ``ollama serve`` detached and wait until the API answers."""
    if is_running(host):
        return True
    log.info("OLLAMA_START binary=%s", binary)
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([str(binary), "serve"], **kwargs)  # noqa: S603
    except OSError as exc:
        log.warning("ollama serve could not be started: %s", exc)
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running(host):
            log.info("OLLAMA_UP version=%s", version(host))
            return True
        time.sleep(1)
    log.warning("ollama did not answer within %ss", timeout)
    return False


# ----------------------------------------------------------------------------- pull
@dataclass
class PullProgress:
    """Latest progress of a model pull."""

    status: str = ""
    completed: int = 0
    total: int = 0

    @property
    def percent(self) -> int:
        """0–100."""
        return int(self.completed * 100 / self.total) if self.total else 0


def pull_model(name: str, on_progress: Callable[[PullProgress], None], host: str = DEFAULT_HOST, timeout: float = PULL_TIMEOUT_S) -> tuple[bool, str]:
    """Stream ``POST /api/pull``; returns (ok, last_status_or_error)."""
    log.info("OLLAMA_PULL model=%s", name)
    body = json.dumps({"name": name, "stream": True}).encode("utf-8")
    req = urllib.request.Request(f"{host}/api/pull", data=body, headers={"Content-Type": "application/json"}, method="POST")
    progress = PullProgress()
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (localhost)
            for raw in resp:
                if time.monotonic() - started > timeout:
                    return False, "timeout"
                try:
                    event = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if "error" in event:
                    log.warning("OLLAMA_PULL_ERROR model=%s error=%s", name, event["error"])
                    return False, str(event["error"])
                progress.status = str(event.get("status", progress.status))
                if "total" in event:
                    progress.total = int(event.get("total") or 0)
                    progress.completed = int(event.get("completed") or 0)
                on_progress(progress)
    except (urllib.error.URLError, OSError) as exc:
        log.warning("OLLAMA_PULL_FAILED model=%s error=%s", name, exc)
        return False, str(exc)
    ok = has_model(name, host)
    log.info("OLLAMA_PULL_DONE model=%s ok=%s status=%s", name, ok, progress.status)
    return ok, progress.status


# ----------------------------------------------------------------------------- install
def install_commands(os_family: str) -> tuple[list[str], bool] | None:
    """(command, needs_elevation) that installs Ollama on this OS, or None when unknown."""
    if os_family == "windows" and shutil.which("winget"):
        return ["winget", "install", "--accept-source-agreements", "--accept-package-agreements", "-e", "--id", "Ollama.Ollama"], False
    if os_family == "macos" and shutil.which("brew"):
        return ["brew", "install", "ollama"], False
    if os_family == "linux":
        # Official installer; it needs root for /usr/local/bin and the systemd unit.
        return ["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"], True
    return None


def install(os_family: str, *, run_elevated: Callable[..., elevate.Elevation] = elevate.run_elevated, password_prompt: str = "") -> tuple[bool, str]:
    """Install Ollama; returns (ok, detail)."""
    plan = install_commands(os_family)
    if plan is None:
        return False, "no installer for this OS"
    cmd, needs_elevation = plan
    log.info("OLLAMA_INSTALL cmd=%s elevated=%s", elevate.shell_join(cmd), needs_elevation)
    if needs_elevation:
        res = run_elevated(cmd, password_prompt=password_prompt, timeout=60 * 20)
        if res.declined:
            return False, "declined"
        if res.outcome is elevate.Outcome.UNAVAILABLE:
            return False, "elevation unavailable"
        return res.ok, res.detail or (res.output[-300:] if res.output else "")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 20, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        return False, repr(exc)
    for line in ((proc.stdout or "") + (proc.stderr or "")).splitlines():
        log.debug("ollama-install| %s", line)
    # winget exit 0x8A15002B (-1978335189) means "already installed" — treat as success.
    ok = proc.returncode in (0, -1978335189)
    return ok, f"exit {proc.returncode}"
