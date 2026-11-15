"""System components (libusb · CUPS · Devanagari fonts): detect what is missing, install with consent.

Stdlib only. One stage: ``SystemComponentsStage``. Per OS it knows which *components* the app can use,
how to detect each one cheaply, and which package-manager command installs it. If anything is missing
and a package manager exists, the owner is asked (yes/no) and ONE elevated command installs all of
them. Declining, or having no package manager, is a WARN with the concrete consequence — never a
stop: the application runs without these components (USB printer / CUPS printing / Hindi glyphs on
PDFs become unavailable until installed).
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bootstrap import elevate
from bootstrap.stages import StageContext, StageResult
from bootstrap.wizard import SKIPPED, Kind, Question

if TYPE_CHECKING:
    from bootstrap.cli import Options

log = logging.getLogger("bootstrap.pkg_manager")

Detector = Callable[[], bool]


@dataclass(frozen=True)
class Component:
    """One optional system component."""

    key: str  # libusb | cups | fonts
    detect: Detector
    packages: tuple[str, ...]  # package-manager package names
    consequence_key: str  # message when missing and not installed


@dataclass(frozen=True)
class Manager:
    """A system package manager and how to drive it non-interactively."""

    name: str  # apt | brew | winget
    install_prefix: tuple[str, ...]
    needs_elevation: bool
    update_prefix: tuple[str, ...] = ()


# ----------------------------------------------------------------------------- detectors
def _exists_any(paths: Sequence[str]) -> bool:
    """True when any of the glob patterns matches (wildcards allowed in every path component)."""
    for pattern in paths:
        if glob.glob(os.path.expanduser(pattern)):
            return True
    return False


def _font_installed(names: Sequence[str]) -> bool:
    """A Devanagari font is present (by well-known file names, or fc-list on Linux)."""
    if shutil.which("fc-list"):
        try:
            out = subprocess.run(["fc-list", ":lang=hi", "family"], capture_output=True, text=True, timeout=10, check=False)  # noqa: S603, S607
            if out.returncode == 0 and out.stdout.strip():
                return True
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("fc-list probe failed: %s", exc)
    return _exists_any(names)


def detect_libusb_linux() -> bool:
    """libusb-1.0 shared library present."""
    return _exists_any(["/usr/lib/*/libusb-1.0.so*", "/usr/lib/libusb-1.0.so*", "/usr/lib64/libusb-1.0.so*", "/lib/*/libusb-1.0.so*"])


def detect_libusb_macos() -> bool:
    """Homebrew libusb present."""
    return _exists_any(["/opt/homebrew/lib/libusb-1.0*.dylib", "/usr/local/lib/libusb-1.0*.dylib"])


def detect_cups() -> bool:
    """CUPS client tools present (lp / lpstat)."""
    return shutil.which("lpstat") is not None and shutil.which("lp") is not None


def detect_fonts_linux() -> bool:
    """Noto / Lohit Devanagari fonts present."""
    return _font_installed(["/usr/share/fonts/truetype/noto/NotoSansDevanagari*.ttf", "/usr/share/fonts/truetype/lohit-devanagari/*.ttf", "/usr/share/fonts/opentype/noto/NotoSansDevanagari*"])


def detect_fonts_macos() -> bool:
    """macOS ships Devanagari Sangam MN / Kohinoor Devanagari."""
    return _exists_any(["/System/Library/Fonts/Supplemental/DevanagariSangamMN*", "/System/Library/Fonts/Supplemental/Kohinoor*", "/System/Library/Fonts/Kohinoor*", "/System/Library/Fonts/DevanagariSangamMN*", str(Path.home() / "Library/Fonts/NotoSansDevanagari*")])


def detect_fonts_windows() -> bool:
    """Windows ships Nirmala UI (Devanagari) since 8.1 — as .ttf on 8.1/10, as a .ttc collection on 11."""
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    return _exists_any([str(windir / "Fonts" / "Nirmala*.tt*"), str(windir / "Fonts" / "mangal*.ttf"), str(Path.home() / "AppData/Local/Microsoft/Windows/Fonts/NotoSansDevanagari*")])


# ----------------------------------------------------------------------------- per-OS catalogue
def components_for(os_family: str) -> list[Component]:
    """Components the app can use on this OS, in install order."""
    if os_family == "linux":
        return [
            Component("libusb", detect_libusb_linux, ("libusb-1.0-0",), "syscomp.consequence.libusb"),
            Component("cups", detect_cups, ("cups", "cups-client"), "syscomp.consequence.cups"),
            Component("fonts", detect_fonts_linux, ("fonts-noto-core", "fonts-lohit-deva"), "syscomp.consequence.fonts"),
        ]
    if os_family == "macos":
        return [
            Component("libusb", detect_libusb_macos, ("libusb",), "syscomp.consequence.libusb"),
            # CUPS and Devanagari fonts ship with macOS; only verified, never installed.
            Component("cups", detect_cups, (), "syscomp.consequence.cups"),
            Component("fonts", detect_fonts_macos, (), "syscomp.consequence.fonts"),
        ]
    if os_family == "windows":
        # libusb: pyusb wheels + Zadig only for raw-USB printers (hardware guide); spooler is built in.
        return [Component("fonts", detect_fonts_windows, (), "syscomp.consequence.fonts")]
    return []


def manager_for(os_family: str) -> Manager | None:
    """The package manager available on this OS, or None."""
    if os_family == "linux" and shutil.which("apt-get"):
        return Manager("apt", ("apt-get", "install", "-y", "--no-install-recommends"), needs_elevation=True, update_prefix=("apt-get", "update"))
    if os_family == "macos" and shutil.which("brew"):
        return Manager("brew", ("brew", "install"), needs_elevation=False)
    if os_family == "windows" and shutil.which("winget"):
        return Manager("winget", ("winget", "install", "--accept-source-agreements", "--accept-package-agreements", "-e", "--id"), needs_elevation=True)
    return None


def install_commands(manager: Manager, packages: Sequence[str]) -> list[list[str]]:
    """Command lines to install ``packages`` with ``manager``."""
    cmds: list[list[str]] = []
    if manager.update_prefix:
        cmds.append(list(manager.update_prefix))
    if manager.name == "winget":
        cmds.extend([*manager.install_prefix, pkg] for pkg in packages)
    else:
        cmds.append([*manager.install_prefix, *packages])
    return cmds


def _run_plain(cmd: list[str], timeout: float = 60 * 20) -> tuple[int, str]:
    log.info("EXEC %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, repr(exc)
    output = (proc.stdout or "") + (proc.stderr or "")
    for line in output.splitlines():
        log.debug("pkg| %s", line)
    log.info("EXEC_EXIT code=%d", proc.returncode)
    return proc.returncode, output


# ----------------------------------------------------------------------------- stage
@dataclass
class SystemComponentsStage:
    """Stage 6: libusb · CUPS · fonts — detect, explain, ask, install once with elevation."""

    key: str = "system_components"
    title_key: str = "stage.system_components"
    os_family: str = field(default_factory=lambda: "windows" if sys.platform.startswith("win") else "macos" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else "unknown")
    run_elevated: Callable[..., elevate.Elevation] = elevate.run_elevated
    run_plain: Callable[[list[str]], tuple[int, str]] = _run_plain

    def applies(self, ctx: StageContext) -> bool:
        """Always; on Windows it only verifies fonts."""
        return True

    def cache_key(self, ctx: StageContext) -> str | None:
        """Detection is cheap — re-run every start (a component may have been installed by hand)."""
        return None

    def run(self, ctx: StageContext) -> StageResult:
        """Detect → explain → ask → install → re-detect."""
        comps = components_for(self.os_family)
        missing = [c for c in comps if not c.detect()]
        log.info("SYSCOMP os=%s checked=%s missing=%s", self.os_family, [c.key for c in comps], [c.key for c in missing])
        if not missing:
            return StageResult.ok(ctx.t("syscomp.all_present", count=len(comps)), missing=[])

        installable = [c for c in missing if c.packages]
        names = ", ".join(ctx.t(f"syscomp.name.{c.key}") for c in missing)
        manager = manager_for(self.os_family)
        if not installable or manager is None:
            hint = " ".join(ctx.t(c.consequence_key) for c in missing)
            if manager is None and installable:
                hint = ctx.t("syscomp.no_manager_hint", packages=" ".join(p for c in installable for p in c.packages)) + " " + hint
            return StageResult.warn(ctx.t("syscomp.missing_not_installable", names=names), error_code="E-SYS-003", hint=hint, missing=[c.key for c in missing])

        packages = [p for c in installable for p in c.packages]
        cmds = install_commands(manager, packages)
        ctx.console.info(ctx.t("syscomp.explain", names=names))
        for cmd in cmds:
            ctx.console.hint(elevate.shell_join(cmd))
        if manager.needs_elevation:
            ctx.console.hint(ctx.t("syscomp.needs_admin"))
        assert ctx.wizard is not None
        answer = ctx.wizard.ask(Question(key="install_system_components", kind=Kind.YESNO, prompt_key="syscomp.ask", default="yes", skippable=False, persist=False))
        if answer in ("no", SKIPPED):
            log.info("SYSCOMP_DECLINED by owner")
            return StageResult.warn(ctx.t("syscomp.declined", names=names), error_code="E-SYS-004", hint=" ".join(ctx.t(c.consequence_key) for c in missing), missing=[c.key for c in missing])

        ctx.console.busy(ctx.t("syscomp.installing", names=names))
        for cmd in cmds:
            if manager.needs_elevation:
                result = self.run_elevated(cmd, password_prompt=ctx.t("syscomp.password_prompt"))
                if result.declined:
                    return StageResult.warn(ctx.t("syscomp.declined", names=names), error_code="E-SYS-004", hint=" ".join(ctx.t(c.consequence_key) for c in missing), missing=[c.key for c in missing])
                if result.outcome is elevate.Outcome.UNAVAILABLE:
                    return StageResult.warn(ctx.t("syscomp.no_elevation", names=names), error_code="E-SYS-004", hint=ctx.t("syscomp.manual_hint", command=elevate.shell_join(cmd)), detail=result.detail, missing=[c.key for c in missing])
                code, detail = result.exit_code if result.exit_code is not None else -1, result.detail
            else:
                code, detail = self.run_plain(cmd)
            if code != 0:
                log.warning("SYSCOMP_INSTALL_FAILED cmd=%s code=%s detail=%s", cmd[0], code, detail[:200])
                return StageResult.warn(ctx.t("syscomp.install_failed", names=names), error_code="E-SYS-003", hint=ctx.t("syscomp.manual_hint", command=elevate.shell_join(cmd)), detail=f"exit {code}: {detail[:300]}", missing=[c.key for c in missing])

        still_missing = [c for c in missing if not c.detect()]
        if still_missing:
            log.warning("SYSCOMP_STILL_MISSING %s", [c.key for c in still_missing])
            return StageResult.warn(ctx.t("syscomp.still_missing", names=", ".join(ctx.t(f"syscomp.name.{c.key}") for c in still_missing)), error_code="E-SYS-003", hint=" ".join(ctx.t(c.consequence_key) for c in still_missing), missing=[c.key for c in still_missing])
        return StageResult.ok(ctx.t("syscomp.installed", names=names), missing=[], installed=[c.key for c in missing])


def register(options: Options) -> SystemComponentsStage:
    """Factory used by ``build_default_pipeline``."""
    return SystemComponentsStage()
