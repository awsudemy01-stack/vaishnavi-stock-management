"""Offline bundle: install on a shop PC without internet, and build the bundle on a connected PC.

Layout (docs/17 §8)::

    offline/
    ├── bundle.json                      # who built it, when, for which OS / Python
    ├── wheels/<os>-<arch>-py<major.minor>/   # all Python packages for that target
    ├── ollama/<os>/                     # Ollama installer (optional)
    └── models/                          # Ollama model archives (optional; imported with `ollama create`)

``--offline <folder>`` validates the bundle up front (``OfflineStage``, runs right after the survey) so
the owner hears "this bundle is for Ubuntu, this PC is Windows" immediately rather than after a
failed pip run. ``--make-offline-bundle <target-os>`` downloads the wheels for the lock file (pip
``download``) and the Ollama installer on a connected PC. Stdlib only.
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bootstrap.cli import Mode, register_mode
from bootstrap.stages import ExitCode, StageContext, StageResult

if TYPE_CHECKING:
    from bootstrap.cli import Options

log = logging.getLogger("bootstrap.offline")

BUNDLE_FILE = "bundle.json"
WHEELS_DIR = "wheels"
OLLAMA_DIR = "ollama"
MODELS_DIR = "models"
LOCK_FILE = "requirements.lock"

OLLAMA_INSTALLERS = {
    "windows": ("https://ollama.com/download/OllamaSetup.exe", "OllamaSetup.exe"),
    "macos": ("https://ollama.com/download/Ollama-darwin.zip", "Ollama-darwin.zip"),
    "linux": ("https://ollama.com/download/ollama-linux-amd64.tgz", "ollama-linux-amd64.tgz"),
}
PIP_PLATFORM_TAGS = {"windows": "win_amd64", "macos": "macosx_11_0_arm64", "linux": "manylinux_2_28_x86_64"}


def os_family() -> str:
    """windows | macos | linux | unknown."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


def arch_tag() -> str:
    """x86_64 | arm64 | …"""
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(m, m)


def target_dir_name(family: str, arch: str, py: str) -> str:
    """Folder name under ``wheels/`` for one target, e.g. ``windows-x86_64-py3.12``."""
    return f"{family}-{arch}-py{py}"


@dataclass
class BundleCheck:
    """Result of validating a bundle for *this* machine."""

    ok: bool
    wheels_dir: Path | None = None
    ollama_installer: Path | None = None
    model_files: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # message keys
    built_for: str = ""


def validate_bundle(bundle: Path, family: str, arch: str, py: str) -> BundleCheck:
    """Check the bundle has wheels for this OS / arch / Python; Ollama parts are optional."""
    check = BundleCheck(ok=False)
    if not bundle.is_dir():
        check.problems.append("offline.err_missing")
        return check
    meta_path = bundle / BUNDLE_FILE
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            check.built_for = ", ".join(meta.get("targets", [])) or str(meta.get("target", ""))
        except (OSError, ValueError) as exc:
            log.debug("bundle.json unreadable: %s", exc)
    wanted = bundle / WHEELS_DIR / target_dir_name(family, arch, py)
    if wanted.is_dir() and any(wanted.glob("*.whl")):
        check.wheels_dir = wanted
    elif (bundle / WHEELS_DIR).is_dir() and any((bundle / WHEELS_DIR).glob("*.whl")):
        check.wheels_dir = bundle / WHEELS_DIR  # flat layout from an older bundle
    else:
        check.problems.append("offline.err_no_wheels")
    installer_dir = bundle / OLLAMA_DIR / family
    if installer_dir.is_dir():
        files = [p for p in installer_dir.iterdir() if p.is_file()]
        check.ollama_installer = files[0] if files else None
    models_dir = bundle / MODELS_DIR
    if models_dir.is_dir():
        check.model_files = sorted(p for p in models_dir.iterdir() if p.is_file())
    check.ok = not check.problems
    log.info("OFFLINE_BUNDLE path=%s ok=%s wheels=%s ollama=%s models=%d problems=%s", bundle, check.ok, check.wheels_dir, check.ollama_installer, len(check.model_files), check.problems)
    return check


# ----------------------------------------------------------------------------- stage
@dataclass
class OfflineStage:
    """Stage 2a (only with ``--offline``): validate the bundle before anything tries to use it."""

    key: str = "offline_bundle"
    title_key: str = "stage.offline_bundle"

    def applies(self, ctx: StageContext) -> bool:
        """Only when ``--offline`` was given."""
        return ctx.options.offline_dir is not None

    def cache_key(self, ctx: StageContext) -> str | None:
        """Cheap; re-check every run."""
        return None

    def run(self, ctx: StageContext) -> StageResult:
        """Validate for this machine; FAIL with a plain-language reason if unusable."""
        assert ctx.options.offline_dir is not None
        py = f"{sys.version_info[0]}.{sys.version_info[1]}"
        check = validate_bundle(ctx.options.offline_dir, os_family(), arch_tag(), py)
        if not check.ok:
            problem = ctx.t(check.problems[0], path=str(ctx.options.offline_dir), target=target_dir_name(os_family(), arch_tag(), py), built_for=check.built_for or "?")
            return StageResult.fail(problem, error_code="E-SYS-015", hint=ctx.t("offline.hint", target=os_family()))
        data = {"wheels_dir": str(check.wheels_dir), "ollama_installer": str(check.ollama_installer) if check.ollama_installer else "", "models": [str(p) for p in check.model_files]}
        note = ctx.t("offline.ok", wheels=len(list(check.wheels_dir.glob("*.whl"))) if check.wheels_dir else 0, ollama="yes" if check.ollama_installer else "no", models=len(check.model_files))
        return StageResult.ok(note, **data)


# ----------------------------------------------------------------------------- bundle builder
def _download(url: str, dest: Path) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as resp, dest.open("wb") as fh:  # noqa: S310 (fixed https URL)
            shutil.copyfileobj(resp, fh)
        return True
    except (urllib.error.URLError, OSError) as exc:
        log.warning("download failed %s: %s", url, exc)
        return False


@register_mode(Mode.MAKE_OFFLINE_BUNDLE)
def run_make_bundle(ctx: StageContext) -> ExitCode:
    """``main.py --make-offline-bundle <windows|macos|linux>`` on a connected PC."""
    target = ctx.options.bundle_target or os_family()
    program_dir = ctx.paths.program_dir
    bundle = program_dir / "offline"
    lock = program_dir / LOCK_FILE
    py = f"{sys.version_info[0]}.{sys.version_info[1]}"
    arch = "arm64" if target == "macos" else "x86_64"
    wheels = bundle / WHEELS_DIR / target_dir_name(target, arch, py)
    wheels.mkdir(parents=True, exist_ok=True)
    ctx.console.info(ctx.t("offline.building", target=target, path=str(bundle)))

    if not lock.exists():
        ctx.console.fail(ctx.t("offline.build_no_lock"))
        return ExitCode.FAIL
    cmd = [sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "-r", str(lock), "-d", str(wheels), "--only-binary=:all:", "--platform", PIP_PLATFORM_TAGS[target], "--python-version", py, "--implementation", "cp"]
    log.info("EXEC %s", " ".join(cmd))
    ctx.console.busy(ctx.t("offline.build_wheels"))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        ctx.console.fail(ctx.t("offline.build_failed") + f"  ({exc})")
        return ExitCode.FAIL
    for line in ((proc.stdout or "") + (proc.stderr or "")).splitlines():
        log.debug("pip| %s", line)
    if proc.returncode != 0:
        ctx.console.fail(ctx.t("offline.build_failed"))
        return ExitCode.FAIL
    ctx.console.ok(ctx.t("offline.build_wheels_ok", count=len(list(wheels.glob("*.whl")))))

    url, name = OLLAMA_INSTALLERS[target]
    ctx.console.busy(ctx.t("offline.build_ollama"))
    if _download(url, bundle / OLLAMA_DIR / target / name):
        ctx.console.ok(ctx.t("offline.build_ollama_ok", name=name))
    else:
        ctx.console.warn(ctx.t("offline.build_ollama_failed"))

    (bundle / MODELS_DIR).mkdir(exist_ok=True)
    meta = {"built_at": datetime.now().isoformat(timespec="seconds"), "built_on": platform.platform(), "targets": [target_dir_name(target, arch, py)], "python": py}
    (bundle / BUNDLE_FILE).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    ctx.console.info(ctx.t("offline.build_models_note", path=str(bundle / MODELS_DIR)))
    ctx.console.ok(ctx.t("offline.build_done", path=str(bundle)))
    return ExitCode.OK


def register(options: Options) -> OfflineStage:
    """Factory used by ``build_default_pipeline``."""
    return OfflineStage()
