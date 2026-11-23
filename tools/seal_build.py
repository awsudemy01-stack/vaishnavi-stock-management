#!/usr/bin/env python3
"""Vaishnavi Sealed Release Builder — turns the "shopping" source project into a single, self-contained
folder that runs on the shop PC WITHOUT exposing the Python source code to whoever sits at the till.

STANDALONE BY DESIGN. This file imports nothing from the project (no ``vaishnavi``, no ``bootstrap``).
It only reads the source tree as plain files on disk and NEVER opens a source file for writing, so it
cannot alter the project it is sealing — that guarantee is also checked mechanically at the end (the
whole source tree is SHA-256 hashed before and after the run; any difference is reported loudly).

What it does, in order (fully interactive — just run ``python tools/seal_build.py``):
  1. Asks for the source project folder (what you called "shopping").
  2. Asks for the output folder (the sealed, distributable result).
  3. Asks for an admin username + a MASKED password (never echoed, never written to any log), and uses
     it to actually verify administrator rights on this machine (not just trust what was typed).
  4. Detects the operating system (Windows / macOS / Linux-Ubuntu) automatically.
  5. Makes sure the tools needed to build the seal are present (pip, the ``venv`` module) and, on
     Ubuntu, offers to install the missing ``python3-venv`` / ``python3-pip`` packages for you.
  6. Copies the source into a private work area and does every destructive step (compiling, deleting
     ``.py`` files) ONLY there — the original source folder is opened read-only, start to finish.
  7. Writes exactly one ``README.md`` in the output folder describing the folder tree and how to run it.
  8. Writes a detailed, timestamped build log under ``<output>/logs/`` (DEBUG on disk, calmer on screen).
  9. Produces ``main.pyc`` + compiled ``.pyc`` packages with the matching ``.py`` files removed from the
     copy — a staff member who opens the sealed folder finds no readable Python source, only bytecode,
     data files (SQL/TOML/JSON/prompts) and a private Python runtime.

HONEST LIMITS (said once here, repeated in the generated README): compiled ``.pyc`` bytecode is NOT the
same as a compiled executable — it hides the source from casual viewing and from anyone without Python
tooling, but a determined expert with a decompiler can still reconstruct approximate source. For a much
harder target, a later pass with PyInstaller or Nuitka can turn this sealed folder into a true native
executable; that needs internet access on this machine and is out of scope for this offline-friendly tool.

Run this tool ON THE MACHINE WHERE THE SEALED FOLDER WILL BE USED. Compiled bytecode is tied to the exact
Python feature version (3.11 vs 3.12 vs 3.13 produce incompatible ``.pyc`` files) — building locally means
the bytecode always matches the interpreter that will run it, with zero guesswork.
"""

from __future__ import annotations

import argparse
import compileall
import ctypes
import getpass
import hashlib
import json
import logging
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOL_NAME = "Vaishnavi Sealed Release Builder"
TOOL_VERSION = "1.0.0"
MIN_PYTHON = (3, 11)

# ----------------------------------------------------------------------------- what travels into the seal
EXCLUDE_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "logs", "data", "backups", "offline", ".nicegui", "tests", "tools", "docs",
}
EXCLUDE_FILE_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sha256"}
# Packages already present in any freshly created venv — never overwritten by a copied-in dependency.
VENV_BOOTSTRAP_NAMES_PREFIXES = ("pip", "setuptools", "wheel", "_distutils_hack", "distutils-precedence")

SEAL_MARKER_FILE = "SEALED_RELEASE.json"  # proves a folder is OUR output, so re-runs can ask before wiping it


class SealBuildError(Exception):
    """A handled, explained failure — printed without a scary traceback."""


# ============================================================================ logging
class _RedactFilter(logging.Filter):
    """Belt-and-braces: even though the password is never passed to a log call, strip it if it ever is."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets and record.msg:
            msg = str(record.msg)
            for s in self._secrets:
                msg = msg.replace(s, "*" * len(s))
            record.msg = msg
        return True


def setup_logging(log_dir: Path, secrets: list[str]) -> Path:
    """DEBUG detail on disk, calmer INFO on screen. Returns the log file path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"seal_build_{stamp}.log"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("  %(message)s"))
    redact = _RedactFilter(secrets)
    file_handler.addFilter(redact)
    console_handler.addFilter(redact)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    return log_path


log = logging.getLogger("seal_build")


def banner() -> None:
    print("=" * 78)
    print(f"{TOOL_NAME}  v{TOOL_VERSION}")
    print("Seals a copy of the source project into one folder with no readable Python source.")
    print("=" * 78)


# ============================================================================ small interactive helpers
def _require_tty() -> None:
    if not sys.stdin.isatty():
        raise SealBuildError(
            "This tool asks interactive questions and must be run in a real terminal "
            "(cmd.exe / PowerShell / Terminal.app / a normal shell) — not redirected input."
        )


def ask_text(prompt: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("  Please type something.")


def ask_yes_no(prompt: str, *, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{prompt} [{hint}]: ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def ask_path(prompt: str, *, default: str | None = None, must_exist: bool = True) -> Path:
    while True:
        text = ask_text(prompt, default=default)
        path = Path(text).expanduser().resolve()
        if must_exist and not path.exists():
            print(f"  '{path}' does not exist. Try again.")
            continue
        return path


def ask_password_confirmed(prompt: str) -> str:
    """Masked input (``getpass`` hides it on every major OS), typed twice to catch typos."""
    while True:
        first = getpass.getpass(f"{prompt}: ")
        if not first:
            print("  Password cannot be empty.")
            continue
        second = getpass.getpass("  Re-type to confirm: ")
        if first != second:
            print("  The two entries did not match — try again.")
            continue
        return first


# ============================================================================ OS detection
@dataclass(frozen=True)
class OsInfo:
    key: str  # "windows" | "macos" | "linux"
    label: str


def detect_os() -> OsInfo:
    system = platform.system()
    if system == "Windows":
        return OsInfo("windows", f"Windows {platform.release()}")
    if system == "Darwin":
        return OsInfo("macos", f"macOS {platform.mac_ver()[0] or ''}".strip())
    if system == "Linux":
        return OsInfo("linux", _linux_distro_label())
    return OsInfo("linux", f"Unrecognised OS ({system}) — treated as Linux")


def _linux_distro_label() -> str:
    os_release = Path("/etc/os-release")
    if os_release.exists():
        try:
            fields = dict(
                line.strip().split("=", 1)
                for line in os_release.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            name = fields.get("PRETTY_NAME", '"Linux"').strip('"')
            return name
        except OSError as exc:
            log.debug("could not read /etc/os-release: %s", exc)
    return "Linux"


# ============================================================================ admin verification
def is_windows_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - ctypes failures vary by environment
        log.debug("IsUserAnAdmin check failed: %s", exc)
        return False


def verify_admin(os_info: OsInfo, password: str) -> tuple[bool, str]:
    """Best-effort REAL verification, not just trusting the typed string.

    Windows has no stdlib API that takes a typed password and elevates a running process, so here the
    password is an audit/confirmation gate and the real check is "is this terminal already elevated?".
    macOS/Linux can genuinely verify the password via ``sudo -S -v`` (refreshes the sudo timestamp,
    no command is executed).
    """
    if os_info.key == "windows":
        if is_windows_admin():
            return True, "this terminal is already running as Administrator"
        return False, (
            "this terminal is NOT running as Administrator. Package installs that need elevation may "
            "fail silently. Right-click cmd.exe/PowerShell -> 'Run as administrator', then run this "
            "tool again from there."
        )
    if shutil.which("sudo") is None:
        log.warning("ADMIN_CHECK sudo not found on this system - trusting the typed password for the audit log only")
        return True, "sudo is not installed here; password accepted for the build record only (not verified)"
    try:
        proc = subprocess.run(
            ["sudo", "-S", "-v"], input=password + "\n", text=True,
            capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run sudo to verify the password: {exc}"
    if proc.returncode == 0:
        return True, "verified via sudo"
    return False, "sudo rejected that password"


# ============================================================================ prerequisite checks / repair
def probe_internet(host: str = "pypi.org", port: int = 443, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        log.info("INTERNET_PROBE unreachable (%s) - continuing fully offline", exc)
        return False


def ensure_venv_module_available(os_info: OsInfo, admin_password: str | None) -> None:
    """Canary: try to create a throwaway venv. Ubuntu's python3 sometimes ships without ``ensurepip``
    unless ``python3-venv``/``python3-pip`` are installed — the exact failure a prior shop-PC run hit."""
    with tempfile.TemporaryDirectory(prefix="seal_venv_check_") as tmp:
        probe_dir = Path(tmp) / "probe"
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(probe_dir)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            log.debug("VENV_MODULE_OK")
            return
        combined = (result.stdout or "") + (result.stderr or "")
        log.warning("VENV_MODULE_FAILED rc=%s output=%s", result.returncode, combined.strip()[-400:])
        if os_info.key == "linux" and "ensurepip" in combined and shutil.which("apt-get"):
            print(
                "\n  This Python cannot create private environments (missing 'python3-venv'/'python3-pip').\n"
                "  Fix: sudo apt-get install -y python3-venv python3-pip"
            )
            if admin_password is not None and ask_yes_no("  Run that command now with the admin password you entered?", default=True):
                fix = subprocess.run(
                    ["sudo", "-S", "apt-get", "install", "-y", "python3-venv", "python3-pip"],
                    input=admin_password + "\n", text=True, capture_output=True, timeout=600,
                )
                log.debug("APT_FIX rc=%s stdout=%s stderr=%s", fix.returncode, fix.stdout[-2000:], fix.stderr[-2000:])
                if fix.returncode == 0:
                    retry = subprocess.run([sys.executable, "-m", "venv", str(probe_dir)], capture_output=True, text=True)
                    if retry.returncode == 0:
                        log.info("VENV_MODULE_REPAIRED")
                        return
            raise SealBuildError(
                "Could not create a private Python environment even after trying to install "
                "python3-venv/python3-pip. Run that command manually, then try again."
            )
        raise SealBuildError(
            "This Python installation cannot create private environments (the 'venv' module failed). "
            "On Ubuntu: sudo apt-get install -y python3-venv python3-pip. On Windows/macOS, reinstall "
            "Python from https://www.python.org/downloads/ ."
        )


def ensure_pip_available() -> None:
    try:
        subprocess.run([sys.executable, "-m", "pip", "--version"], check=True, capture_output=True, text=True)
        log.debug("PIP_OK")
    except (subprocess.CalledProcessError, OSError) as exc:
        log.warning("PIP_MISSING (%s) - trying ensurepip", exc)
        try:
            subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"], check=True, capture_output=True, text=True)
            log.info("PIP_REPAIRED via ensurepip")
        except (subprocess.CalledProcessError, OSError) as exc2:
            raise SealBuildError(f"pip is not available and 'ensurepip' could not install it: {exc2}") from exc2


# ============================================================================ hashing (proof source is untouched)
def iter_sealable_files(root: Path):
    """Every file that is part of what we read/copy — same rule used for hashing and for copying."""
    for current_root, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIR_NAMES and not d.endswith(".egg-info"))
        for name in sorted(files):
            if any(name.endswith(suf) for suf in EXCLUDE_FILE_SUFFIXES):
                continue
            yield Path(current_root) / name


def hash_tree(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in iter_sealable_files(root):
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        digests[str(path.relative_to(root).as_posix())] = h.hexdigest()
    return digests


def verify_source_untouched(before: dict[str, str], after: dict[str, str]) -> list[str]:
    problems = []
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(p for p in (set(before) & set(after)) if before[p] != after[p])
    problems += [f"NEW file appeared: {p}" for p in added]
    problems += [f"file disappeared: {p}" for p in removed]
    problems += [f"file content changed: {p}" for p in changed]
    return problems


# ============================================================================ the seal itself
@dataclass
class BuildPlan:
    source: Path
    output: Path
    os_info: OsInfo
    admin_user: str
    dependency_mode: str = "pending"  # reuse-venv | fresh-venv | offline-no-deps
    compiled_count: int = 0
    deleted_py_count: int = 0
    data_file_count: int = 0


def sanity_check_source(source: Path) -> None:
    expected = ["main.py", "pyproject.toml", "src"]
    missing = [name for name in expected if not (source / name).exists()]
    if missing:
        raise SealBuildError(
            f"'{source}' does not look like the Vaishnavi project folder (missing: {', '.join(missing)}). "
            "Point this tool at the 'shopping' folder that contains main.py."
        )
    log.debug("SOURCE_SANITY_OK %s", source)


def guard_output_not_inside_source(source: Path, output: Path) -> None:
    try:
        output.relative_to(source)
        raise SealBuildError(
            "The output folder must NOT be inside the source project folder — that would risk writing "
            "into the very codebase this tool promises never to touch. Choose a separate location."
        )
    except ValueError:
        pass  # good: output is not inside source
    if output == source:
        raise SealBuildError("Output folder cannot be the same as the source folder.")


def prepare_output_dir(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        marker = output / SEAL_MARKER_FILE
        if marker.exists():
            if not ask_yes_no(f"'{output}' already contains a previous sealed build. Wipe and rebuild?", default=False):
                raise SealBuildError("Cancelled — choose an empty output folder to proceed.")
            shutil.rmtree(output)
        else:
            raise SealBuildError(
                f"'{output}' already exists and is not empty, and is not a previous sealed build "
                "(no SEALED_RELEASE.json marker found). Choose an empty or new folder, to avoid "
                "overwriting something unrelated."
            )
    output.mkdir(parents=True, exist_ok=True)


def copy_source_into_workdir(source: Path, app_dir: Path) -> int:
    """Read-only on the source side: every open() on a source path is 'rb'. Returns files copied."""
    count = 0
    for src_file in iter_sealable_files(source):
        rel = src_file.relative_to(source)
        dst_file = app_dir / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        with src_file.open("rb") as fh_in, dst_file.open("wb") as fh_out:
            shutil.copyfileobj(fh_in, fh_out)
        count += 1
    log.info("SOURCE_COPIED files=%d -> %s", count, app_dir)
    return count


def compile_and_strip_sources(app_dir: Path, source_guard: Path) -> tuple[int, int]:
    """Compile every .py under app_dir to legacy '<name>.pyc' beside it, then delete the .py.

    ``source_guard`` is the ORIGINAL source folder; this function refuses to run anywhere under it,
    as a hard safety net on top of the fact that app_dir is always a fresh copy.
    """
    app_dir = app_dir.resolve()
    if app_dir == source_guard or source_guard in app_dir.parents:
        raise SealBuildError("Internal safety check failed: refusing to compile inside the source folder.")
    py_files = [p for p in app_dir.rglob("*.py")]
    # workers=1 (the default) compiles sequentially in-process — deliberately avoid compileall's
    # multiprocessing path (workers=0 / >1), which spawns a process pool and is fragile in a frozen or
    # embedded interpreter; a one-off build does not need the speed.
    ok = compileall.compile_dir(str(app_dir), legacy=True, force=True, quiet=1, workers=1)
    if not ok:
        raise SealBuildError("Compiling the copied source to bytecode failed — see the log file for details.")
    compiled = 0
    for py_file in py_files:
        pyc_file = py_file.with_suffix(".pyc")
        if not pyc_file.exists() or pyc_file.stat().st_size == 0:
            raise SealBuildError(f"Expected compiled file missing, refusing to delete source: {py_file}")
        py_file.unlink()
        compiled += 1
    log.info("COMPILE_AND_STRIP compiled=%d", compiled)
    return compiled, len(py_files) - compiled


def read_dependencies(pyproject: Path) -> list[str]:
    try:
        import tomllib  # py311+, stdlib
    except ImportError as exc:  # pragma: no cover - MIN_PYTHON already guards this
        raise SealBuildError("This tool needs Python 3.11+ (for tomllib).") from exc
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return list(data.get("project", {}).get("dependencies", []))


def site_packages_of(venv_dir: Path) -> Path | None:
    windows_candidate = venv_dir / "Lib" / "site-packages"
    if windows_candidate.exists():
        return windows_candidate
    lib_dir = venv_dir / "lib"
    if lib_dir.exists():
        for child in sorted(lib_dir.glob("python3.*")):
            candidate = child / "site-packages"
            if candidate.exists():
                return candidate
    return None


def runtime_python(runtime_dir: Path, os_info: OsInfo) -> Path:
    return runtime_dir / "Scripts" / "python.exe" if os_info.key == "windows" else runtime_dir / "bin" / "python3"


def build_runtime(source: Path, output: Path, os_info: OsInfo, plan: BuildPlan) -> Path:
    """A fresh venv in the OUTPUT (never the source), populated from the source's existing .venv when
    present (fast, offline, already proven to work) or by pip install when online."""
    runtime_dir = output / "runtime"
    log.info("RUNTIME_CREATE %s", runtime_dir)
    subprocess.run([sys.executable, "-m", "venv", str(runtime_dir)], check=True, capture_output=True, text=True)
    dst_site = site_packages_of(runtime_dir)
    if dst_site is None:
        raise SealBuildError("The new runtime environment has no site-packages folder — venv creation did not finish correctly.")

    existing_venv = source / ".venv"
    src_site = site_packages_of(existing_venv) if existing_venv.exists() else None
    if src_site is not None:
        plan.dependency_mode = "reuse-venv"
        log.info("DEPENDENCIES_REUSE from=%s", src_site)
        for entry in sorted(src_site.iterdir()):
            if entry.name.startswith(VENV_BOOTSTRAP_NAMES_PREFIXES) or entry.name == "__pycache__":
                continue
            dest = dst_site / entry.name
            if entry.is_dir():
                shutil.copytree(entry, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, dest)
        return runtime_dir

    if probe_internet():
        plan.dependency_mode = "fresh-venv"
        runtime_pip = runtime_python(runtime_dir, os_info)
        lock_file = source / "requirements.lock"
        if lock_file.exists():
            log.info("DEPENDENCIES_INSTALL from=requirements.lock")
            cmd = [str(runtime_pip), "-m", "pip", "install", "--no-deps", "-r", str(lock_file)]
        else:
            deps = read_dependencies(source / "pyproject.toml")
            log.info("DEPENDENCIES_INSTALL from=pyproject.toml count=%d", len(deps))
            cmd = [str(runtime_pip), "-m", "pip", "install", *deps]
        result = subprocess.run(cmd, capture_output=True, text=True)
        log.debug("PIP_INSTALL rc=%s stdout=%s stderr=%s", result.returncode, result.stdout[-3000:], result.stderr[-3000:])
        if result.returncode != 0:
            log.warning("DEPENDENCIES_INSTALL_FAILED - the sealed app may be missing packages until fixed")
        return runtime_dir

    plan.dependency_mode = "offline-no-deps"
    log.warning(
        "DEPENDENCIES_NONE no existing .venv found in the source and no internet - the sealed app will "
        "start but report missing packages. Run the normal installer (main.py) once on this machine "
        "first (it creates .venv), then re-run this sealing tool."
    )
    return runtime_dir


# ============================================================================ launchers + docs
def write_build_info(output: Path, plan: BuildPlan) -> dict[str, Any]:
    info = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "built_by": plan.admin_user,
        "built_on_os": plan.os_info.label,
        "python_version": platform.python_version(),
        "dependency_mode": plan.dependency_mode,
        "compiled_files": plan.compiled_count,
        "data_files": plan.data_file_count,
    }
    (output / SEAL_MARKER_FILE).write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def write_launchers(output: Path, os_info: OsInfo) -> None:
    app = "app"
    runtime_win = r"runtime\Scripts\python.exe"
    runtime_posix = "runtime/bin/python3"

    bat = output / "Start VaishnaviStore.bat"
    bat.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "chcp 65001 >nul\r\n"
        "set PYTHONIOENCODING=utf-8\r\n"
        "set PYTHONUTF8=1\r\n"
        f"set PYTHONPATH={app}\\src;{app}\r\n"
        f"if not exist \"{runtime_win}\" (\r\n"
        "  echo Sealed runtime not found - re-run tools\\seal_build.py to rebuild this folder.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        f"\"{runtime_win}\" \"{app}\\main.pyc\" %*\r\n"
        "if errorlevel 1 pause\r\n",
        encoding="ascii",
    )

    command = output / "Start VaishnaviStore.command"
    command.write_text(
        "#!/bin/bash\n"
        "cd \"$(dirname \"$0\")\" || exit 1\n"
        "export PYTHONIOENCODING=utf-8 PYTHONUTF8=1\n"
        f"export PYTHONPATH=\"{app}/src:{app}\"\n"
        f"if [ ! -x \"{runtime_posix}\" ]; then\n"
        "  echo 'Sealed runtime not found - re-run tools/seal_build.py to rebuild this folder.'\n"
        "  read -r -p 'Press Enter to close.' _\n"
        "  exit 1\n"
        "fi\n"
        f"\"{runtime_posix}\" \"{app}/main.pyc\" \"$@\"\n"
        "CODE=$?\n"
        "if [ \"$CODE\" -ne 0 ]; then read -r -p 'Press Enter to close.' _; fi\n"
        "exit \"$CODE\"\n",
        encoding="ascii",
    )
    command.chmod(command.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    sh = output / "start-vaishnavistore.sh"
    sh.write_text(command.read_text(encoding="ascii"), encoding="ascii")
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    log.info("LAUNCHERS_WRITTEN windows=%s macos=%s linux=%s", bat.name, command.name, sh.name)


def render_tree(root: Path, *, prefix: str = "", max_children: int = 20) -> list[str]:
    lines: list[str] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return lines
    shown = entries[:max_children]
    for i, entry in enumerate(shown):
        is_last = i == len(shown) - 1 and len(entries) <= max_children
        connector = "`-- " if is_last else "|-- "
        lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
        if entry.is_dir():
            extension = "    " if is_last else "|   "
            lines.extend(render_tree(entry, prefix=prefix + extension, max_children=max_children))
    if len(entries) > max_children:
        lines.append(f"{prefix}`-- ... {len(entries) - max_children} more item(s) not shown")
    return lines


def write_readme(output: Path, plan: BuildPlan, info: dict[str, Any], hash_report: list[str]) -> None:
    tree_lines = render_tree(output)
    hash_block = (
        "No differences detected — the source project was not modified."
        if not hash_report
        else "WARNING — differences were detected (should never happen):\n  " + "\n  ".join(hash_report)
    )
    content = rf"""# {TOOL_NAME} — sealed release

This folder is a **self-contained, ready-to-run copy** of Vaishnavi Store Manager. It was produced by
`tools/seal_build.py` so that billing-counter staff can run the application **without being able to read
its Python source code**. The original project folder was opened read-only and was not changed in any
way (see "Source integrity check" below).

## What is in here, and why

| Item | Purpose |
|---|---|
| `app/` | The application itself, compiled to Python **bytecode (`.pyc`)**. The original `.py` source files were deleted from this copy after compiling (the original project is untouched). Non-code data files (`.sql`, `.toml`, `.json`, prompt `.md` files) are copied as-is because they are configuration/data, not program logic. |
| `runtime/` | A private Python environment with every library the app needs, so nothing has to be installed system-wide. |
| `Start VaishnaviStore.bat` | Double-click on **Windows**. |
| `Start VaishnaviStore.command` | Double-click on **macOS** (first time: right-click → Open). |
| `start-vaishnavistore.sh` | Run on **Ubuntu/Linux**: `./start-vaishnavistore.sh` |
| `logs/` | This builder's own debug log (for fixing build problems — the running app's own logs go to its usual data location, unchanged). |
| `SEALED_RELEASE.json` | Build record: when, by whom, on what OS/Python, how dependencies were obtained. |

## How to run it
1. Copy this **whole folder** to the shop PC (or build it directly there — see "Rebuilding" below).
2. Double-click the launcher for that computer's OS (table above).
3. The app starts exactly as it does from the normal source checkout — same checks, same database,
   same behaviour. Nothing about how the application *runs* has changed, only how it is *packaged*.

## Folder tree
```
{chr(10).join(tree_lines) if tree_lines else "(empty)"}
```

## Build record
- Built at (UTC): **{info['built_at_utc']}**
- Built by (admin login typed at build time): **{info['built_by']}**
- Built on: **{info['built_on_os']}**, Python **{info['python_version']}**
- Dependency source: **{info['dependency_mode']}** (`reuse-venv` = copied from the project's own `.venv`;
  `fresh-venv` = installed from the internet during this build; `offline-no-deps` = none available —
  see the log for what to do next)
- Compiled Python files: **{info['compiled_files']}** · data files copied as-is: **{info['data_files']}**

## Source integrity check
Before and after sealing, every file in the source project was SHA-256 hashed. Result:

{hash_block}

## Rebuilding / updating this sealed folder
Run `python tools/seal_build.py` again from the source project whenever the source code changes. It is
always safe to re-run: the source project is only ever read, this output folder is what gets rebuilt
(you will be asked before an existing sealed folder is wiped).

Rebuild **on the same computer (or same OS + Python version) that will run it** — compiled bytecode is
tied to the exact Python version it was compiled with; building directly on the target machine avoids
any version-mismatch guesswork.

## Honest limits — what this does and does not protect against
- This hides the source from **casual viewing** (opening the folder in a text editor, browsing on a
  USB drive, etc.) and from anyone without Python decompiling tools. It is **not** unbreakable: a
  determined expert with a `.pyc` decompiler can reconstruct an approximation of the original source.
- If the app ever prints a Python error traceback, the lines will still show original file/folder
  **names** (e.g. `app\main.py`, `bootstrap\cli.py`) — Python keeps that string inside every compiled
  file for error messages. These are the same names already visible in the folder listing; no actual
  program logic is revealed by this.
- Configuration/data files (SQL schema, TOML settings templates, AI prompt text) are intentionally left
  as plain text — they are not proprietary algorithms, and the app needs to read them as data at runtime.
- For a substantially harder target (a true native executable), a later step with PyInstaller or Nuitka
  can turn this sealed folder into a single compiled binary. That needs internet access to install the
  build tool and the app's own dependencies, so it was not attempted automatically here.

## Troubleshooting
- Nothing happens when you double-click the launcher → open `logs/` in **this** folder for the build
  log, and the application's own log folder (printed in the terminal when it starts) for a run-time
  issue.
- "Sealed runtime not found" → the `runtime/` folder is missing or incomplete; re-run `seal_build.py`.
- A Python version warning when starting → rebuild on this exact machine (see "Rebuilding" above).
"""
    (output / "README.md").write_text(content, encoding="utf-8")
    log.info("README_WRITTEN %s", output / "README.md")


# ============================================================================ main flow
def run(args: argparse.Namespace) -> int:
    _require_tty()
    banner()

    source = Path(args.source).expanduser().resolve() if args.source else ask_path(
        "Path to the 'shopping' source project folder", must_exist=True
    )
    sanity_check_source(source)

    output = Path(args.output).expanduser().resolve() if args.output else ask_path(
        "Path to the OUTPUT folder for the sealed build (will be created)", default=str(Path.cwd() / "VaishnaviStore"), must_exist=False
    )
    guard_output_not_inside_source(source, output)
    prepare_output_dir(output)

    admin_user = args.admin_user or ask_text("Admin username (for the build record)")
    admin_password = args.admin_password if args.admin_password is not None else ask_password_confirmed("Admin password")

    log_path = setup_logging(output / "logs", secrets=[admin_password])
    print(f"\nDetailed log: {log_path}\n")
    log.info("SEAL_BUILD_START tool_version=%s source=%s output=%s", TOOL_VERSION, source, output)

    os_info = detect_os()
    log.info("OS_DETECTED %s (%s)", os_info.label, os_info.key)

    ok, detail = verify_admin(os_info, admin_password)
    log.info("ADMIN_VERIFY ok=%s detail=%s", ok, detail)
    if not ok:
        print(f"\n  Admin check: {detail}")
        if not ask_yes_no("Continue anyway? (some install steps may fail)", default=False):
            raise SealBuildError("Cancelled at the admin verification step.")

    print("\nChecking build prerequisites (pip, the Python 'venv' module) ...")
    ensure_pip_available()
    ensure_venv_module_available(os_info, admin_password)
    log.info("PREREQS_OK")

    plan = BuildPlan(source=source, output=output, os_info=os_info, admin_user=admin_user)

    print("Hashing the source project (so we can prove nothing was changed) ...")
    before = hash_tree(source)
    log.info("SOURCE_HASHED files=%d", len(before))

    app_dir = output / "app"
    print("Copying the source into a private work area (source stays read-only) ...")
    total_copied = copy_source_into_workdir(source, app_dir)

    print("Compiling to bytecode and removing the readable .py copies ...")
    compiled, remaining = compile_and_strip_sources(app_dir, source_guard=source)
    plan.compiled_count = compiled
    plan.data_file_count = total_copied - compiled
    if remaining:
        log.warning("COMPILE_INCOMPLETE remaining=%d", remaining)

    print("Preparing the private Python runtime (this can take a while the first time) ...")
    build_runtime(source, output, os_info, plan)

    print("Writing launchers, build record and README ...")
    write_launchers(output, os_info)
    info = write_build_info(output, plan)

    print("Re-hashing the source project to confirm it was not touched ...")
    after = hash_tree(source)
    hash_report = verify_source_untouched(before, after)
    if hash_report:
        log.critical("SOURCE_MODIFIED issues=%s", hash_report)
    else:
        log.info("SOURCE_UNTOUCHED_VERIFIED files=%d", len(after))

    write_readme(output, plan, info, hash_report)

    print("\n" + "=" * 78)
    if hash_report:
        print("WARNING: the source-integrity check found differences — see the README and log file.")
    else:
        print("Source project verified UNCHANGED.")
    print(f"Sealed release ready: {output}")
    print(f"Build log: {log_path}")
    print("=" * 78)
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        sys.stderr.write(
            f"This tool needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ "
            f"(found {sys.version_info.major}.{sys.version_info.minor}).\n"
        )
        return 2

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--source", help="Path to the source project (skips the interactive prompt).")
    parser.add_argument("--output", help="Path to the output folder (skips the interactive prompt).")
    parser.add_argument("--admin-user", help="Admin username (skips the interactive prompt).")
    parser.add_argument("--admin-password", help="Admin password — avoid using this on a shared machine; prefer the masked prompt.")
    args = parser.parse_args(argv)

    try:
        return run(args)
    except SealBuildError as exc:
        log.error("SEAL_BUILD_FAILED %s", exc)
        print(f"\nStopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        log.info("SEAL_BUILD_CANCELLED by user")
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 - last-resort guard, full detail goes to the log file
        log.critical("SEAL_BUILD_CRASHED\n%s", traceback.format_exc())
        print("\nAn unexpected error occurred. Full details are in the log file above.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
