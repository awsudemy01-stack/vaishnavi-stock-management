"""Python version check, private virtual environment and package installation (stdlib only).

Three stages:

* ``python_check`` — the running interpreter is 3.11–3.13 and can create virtual environments
  (Ubuntu ships Python without ``python3-venv``; that is the most common real-world failure).
* ``venv`` — ``.venv`` next to ``main.py``; rebuilt when it was created by a different interpreter.
* ``packages`` — ``pip install`` from the hash-pinned ``requirements.lock`` (or from ``pyproject.toml``
  with a WARN when the lock is absent), optionally from an offline wheelhouse; pip's output streams
  into the install log while the owner sees a counting progress line; the result is verified by
  importing the key packages *inside* the venv.

Cache: the ``packages`` stage is skipped when the lock/pyproject hash and interpreter are unchanged,
so a daily start spends milliseconds here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import venv
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bootstrap.stages import StageContext, StageResult, StageStatus

if TYPE_CHECKING:
    from bootstrap.cli import Options

log = logging.getLogger("bootstrap.python_env")

MIN_PYTHON = (3, 11)
MAX_TESTED_PYTHON = (3, 13)
VENV_DIR = ".venv"
LOCK_FILE = "requirements.lock"
PYPROJECT = "pyproject.toml"
WHEELS_DIR = "wheels"
VERIFY_IMPORTS = ("nicegui", "fastapi", "sqlalchemy", "alembic", "pydantic", "cryptography", "keyring")
PIP_TIMEOUT_S = 60 * 40  # a cold install on slow shop internet
_COLLECTING = re.compile(r"^\s*(Collecting|Downloading|Processing|Installing collected packages)", re.I)
_INSTALLED = re.compile(r"^\s*Successfully installed (.+)$", re.I)
_NETWORK_ERROR = re.compile(r"NewConnectionError|getaddrinfo failed|Connection refused|ProxyError|Max retries exceeded|Temporary failure in name resolution|SSLError", re.I)


# ----------------------------------------------------------------------------- helpers
def version_tuple(info: tuple[int, ...] | None = None) -> tuple[int, int]:
    """(major, minor) of the given or running interpreter."""
    v = info or tuple(sys.version_info[:2])
    return int(v[0]), int(v[1])


def venv_python(venv_dir: Path) -> Path:
    """Interpreter path inside a venv (Windows vs POSIX layout)."""
    if sys.platform.startswith("win"):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def venv_home(venv_dir: Path) -> str | None:
    """``home =`` entry of ``pyvenv.cfg`` (the interpreter that built the venv)."""
    cfg = venv_dir / "pyvenv.cfg"
    if not cfg.exists():
        return None
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lower().startswith("home"):
            return line.split("=", 1)[1].strip()
    return None


def current_home() -> str:
    """Directory of the running interpreter, as ``venv`` would record it."""
    base = getattr(sys, "_base_executable", None) or sys.executable
    return str(Path(base).resolve().parent)


def file_digest(path: Path) -> str:
    """Short SHA-256 of a file ('' when missing)."""
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def pip_command(python: Path, program_dir: Path, *, lock: Path | None, wheels: Path | None) -> list[list[str]]:
    """The pip invocations for this install, in order."""
    base = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input"]
    offline = ["--no-index", "--find-links", str(wheels)] if wheels else []
    if lock is not None:
        return [
            base + offline + ["--require-hashes", "-r", str(lock)],
            base + offline + ["--no-deps", "-e", str(program_dir)],
        ]
    return [base + offline + ["-e", str(program_dir)]]


def run_streaming(cmd: list[str], *, on_line: Callable[[str], None], timeout: float = PIP_TIMEOUT_S, env: dict[str, str] | None = None) -> int:
    """Run ``cmd`` streaming each output line to ``on_line`` and the log; returns the exit code.

    Output is drained by a helper thread so the deadline applies even when the child is silent
    (a hung pip on a flaky proxy used to block ``readline()`` forever). ``-1`` means timed out.
    """
    log.info("EXEC %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env)  # noqa: S603
    except OSError as exc:
        log.error("EXEC_START_FAILED %s: %s", cmd[0], exc)
        return -2
    assert proc.stdout is not None
    stream = proc.stdout

    def drain() -> None:
        try:
            for raw in iter(stream.readline, ""):
                line = raw.rstrip()
                if line:
                    log.debug("pip| %s", line)
                    try:
                        on_line(line)
                    except Exception as exc:  # a progress-display bug must not kill the install
                        log.debug("on_line callback failed: %r", exc)
        except (OSError, ValueError) as exc:
            log.debug("EXEC_DRAIN_END %r", exc)

    reader = threading.Thread(target=drain, name="pip-output", daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
        log.error("EXEC_TIMEOUT after %ss: %s", timeout, cmd[0])
        code = -1
    finally:
        reader.join(timeout=5)
        try:
            stream.close()
        except OSError:
            pass
    log.info("EXEC_EXIT code=%d", code)
    return code


def count_requirements(lock: Path) -> int:
    """Number of requirement lines in a pip requirements file (for the progress denominator)."""
    n = 0
    for line in lock.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s and not s.startswith(("#", "--", "\\")) and "==" in s:
            n += 1
    return n


_VERIFY_SCRIPT = """
import importlib, sys
bad = []
for m in sys.argv[1:]:
    try:
        importlib.import_module(m)
    except Exception as exc:  # ImportError, missing DLL, broken wheel ...
        bad.append(m)
        sys.stderr.write("%s: %r\\n" % (m, exc))
print(" ".join(bad))
"""


def verify_imports(python: Path, modules: tuple[str, ...] = VERIFY_IMPORTS) -> list[str]:
    """Really import each module inside the venv; returns the names that fail (reasons in the log)."""
    try:
        out = subprocess.run([str(python), "-c", _VERIFY_SCRIPT, *modules], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("verify_imports failed to run: %s", exc)
        return list(modules)
    if out.stderr.strip():
        for line in out.stderr.strip().splitlines()[:40]:
            log.warning("verify_imports: %s", line)
    if out.returncode != 0:
        log.warning("verify_imports exit=%d", out.returncode)
        return list(modules)
    missing = out.stdout.split()
    log.debug("VERIFY_IMPORTS checked=%d missing=%s", len(modules), missing or "-")
    return missing


# ----------------------------------------------------------------------------- stages
@dataclass
class PythonCheckStage:
    """Stage 3: is this interpreter usable?"""

    key: str = "python_check"
    title_key: str = "stage.python_check"

    def applies(self, ctx: StageContext) -> bool:
        """Always."""
        return True

    def cache_key(self, ctx: StageContext) -> str | None:
        """Cheap — re-check every run."""
        return None

    def run(self, ctx: StageContext) -> StageResult:
        """Version window and ``venv`` availability."""
        major, minor = version_tuple()
        label = f"{major}.{minor}"
        if (major, minor) < MIN_PYTHON:
            return StageResult.fail(ctx.t("python.too_old", found=label, need=f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"), error_code="E-SYS-001", hint=ctx.t("python.install_hint"))
        try:
            import ensurepip  # noqa: F401, PLC0415
        except ImportError:
            log.warning("ensurepip missing (Debian/Ubuntu without python3-venv) code=E-SYS-001")
            return StageResult.fail(ctx.t("python.no_venv"), error_code="E-SYS-001", hint=ctx.t("python.venv_hint"))
        if (major, minor) > MAX_TESTED_PYTHON:
            return StageResult.warn(ctx.t("python.untested", found=label), error_code="E-SYS-011", version=label, executable=sys.executable)
        return StageResult.ok(ctx.t("python.ok", found=label), version=label, executable=sys.executable)


@dataclass
class VenvStage:
    """Stage 4: private environment ``.venv`` next to ``main.py``."""

    key: str = "venv"
    title_key: str = "stage.venv"

    def applies(self, ctx: StageContext) -> bool:
        """Always."""
        return True

    def cache_key(self, ctx: StageContext) -> str | None:
        """Not cached — existence and ownership are verified on every run (fast)."""
        return None

    def run(self, ctx: StageContext) -> StageResult:
        """Create or reuse; rebuild when built by another interpreter or broken."""
        venv_dir = ctx.paths.program_dir / VENV_DIR
        python = venv_python(venv_dir)
        if python.exists():
            home = venv_home(venv_dir)
            if home and Path(home).resolve() == Path(current_home()).resolve():
                return StageResult.ok(ctx.t("venv.reused"), python=str(python), created=False)
            log.warning("venv built by another interpreter (home=%s, now=%s) — rebuilding", home, current_home())
            ctx.console.busy(ctx.t("venv.rebuilding"))
            shutil.rmtree(venv_dir, ignore_errors=True)
        elif venv_dir.exists():
            log.warning("venv folder exists without interpreter — rebuilding")
            shutil.rmtree(venv_dir, ignore_errors=True)
        else:
            ctx.console.busy(ctx.t("venv.creating"))
        try:
            venv.EnvBuilder(with_pip=True, clear=True, symlinks=not sys.platform.startswith("win"), upgrade_deps=False).create(venv_dir)
        except Exception as exc:
            log.exception("venv creation failed")
            return StageResult.fail(ctx.t("venv.failed"), error_code="E-SYS-002", detail=repr(exc), hint=ctx.t("venv.failed_hint", path=str(venv_dir)))
        if not python.exists():
            return StageResult.fail(ctx.t("venv.failed"), error_code="E-SYS-002", detail="interpreter missing after create", hint=ctx.t("venv.failed_hint", path=str(venv_dir)))
        log.info("VENV_CREATED path=%s python=%s", venv_dir, python)
        return StageResult.ok(ctx.t("venv.created"), python=str(python), created=True)


@dataclass
class PackagesStage:
    """Stage 5: install the application and its dependencies into the venv."""

    key: str = "packages"
    title_key: str = "stage.packages"

    def applies(self, ctx: StageContext) -> bool:
        """Only when the venv stage produced an interpreter."""
        return bool(ctx.result_data("venv").get("python"))

    def cache_key(self, ctx: StageContext) -> str | None:
        """Never skipped by the runner: imports are verified on **every** start (the PC is switched off
        nightly and anything can have changed); pip itself runs only when :meth:`inputs_key` changed."""
        return None

    def inputs_key(self, ctx: StageContext) -> str:
        """Lock/pyproject content + venv identity (``pyvenv.cfg``) + offline bundle path."""
        program_dir = ctx.paths.program_dir
        python = ctx.result_data("venv").get("python", "")
        venv_cfg = Path(python).resolve().parent.parent / "pyvenv.cfg" if python else Path()
        offline = str(ctx.options.offline_dir or "")
        return f"{file_digest(program_dir / LOCK_FILE)}|{file_digest(program_dir / PYPROJECT)}|{python}|{file_digest(venv_cfg)}|{offline}"

    def run(self, ctx: StageContext) -> StageResult:
        """pip install with streaming progress (when needed); verify real imports every time."""
        program_dir = ctx.paths.program_dir
        python = Path(ctx.result_data("venv")["python"])
        venv_rebuilt = bool(ctx.result_data("venv").get("created"))
        inputs = self.inputs_key(ctx)
        previous = ctx.state.stage_record(self.key) or {}
        previous_data = previous.get("data") if isinstance(previous.get("data"), dict) else {}
        if not venv_rebuilt and previous.get("status") in (StageStatus.OK.value, StageStatus.WARN.value) and previous_data.get("inputs_key") == inputs:
            missing = verify_imports(python)
            if not missing:
                log.info("PACKAGES_VERIFIED cached=true hash_verified=%s", previous_data.get("hash_verified"))
                # Re-build the data instead of spreading the previous record: it already carries ``verified_only``
                # from the day before, and ``**previous_data, verified_only=True`` raised TypeError on every
                # second cached start (live run 3, E-SYS-099 in the packages stage).
                data = {k: v for k, v in previous_data.items() if k != "verified_only"}
                data["verified_only"] = True
                if previous_data.get("hash_verified") is False:
                    return StageResult.warn(ctx.t("packages.verified", count=len(VERIFY_IMPORTS)), error_code="E-SYS-041", hint=ctx.t("packages.unpinned_hint"), **data)
                return StageResult.ok(ctx.t("packages.verified", count=len(VERIFY_IMPORTS)), **data)
            log.warning("PACKAGES_REVERIFY_FAILED missing=%s — reinstalling", missing)
        else:
            log.info("PACKAGES_INSTALL_NEEDED rebuilt=%s changed=%s", venv_rebuilt, previous_data.get("inputs_key") != inputs)
        lock = program_dir / LOCK_FILE
        has_lock = lock.exists()
        wheels: Path | None = None
        if ctx.options.offline_dir:
            validated = ctx.result_data("offline_bundle").get("wheels_dir")
            wheels = Path(validated) if validated else ctx.options.offline_dir / WHEELS_DIR
        if wheels and not wheels.is_dir():
            return StageResult.fail(ctx.t("packages.no_wheels", path=str(wheels)), error_code="E-SYS-002", hint=ctx.t("packages.no_wheels_hint"))
        total = count_requirements(lock) if has_lock else 0
        label = ctx.t("packages.installing", count=total) if total else ctx.t("packages.installing_unknown")

        seen: set[str] = set()
        installed: list[str] = []
        network_trouble = False

        def on_line(line: str) -> None:
            nonlocal network_trouble
            if _COLLECTING.match(line):
                name = line.split()[1] if len(line.split()) > 1 else line
                seen.add(name)
                if total:
                    ctx.console.progress(label, min(len(seen), total), total)
            if _NETWORK_ERROR.search(line):
                network_trouble = True
            m = _INSTALLED.match(line)
            if m:
                installed.extend(m.group(1).split())

        if not total:
            ctx.console.busy(label)
        env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONIOENCODING="utf-8")
        for cmd in pip_command(python, program_dir, lock=lock if has_lock else None, wheels=wheels):
            code = run_streaming(cmd, on_line=on_line, env=env)
            if code != 0:
                if total:
                    ctx.console.progress_end()
                if wheels:
                    hint_key = "packages.failed_offline_hint"
                elif network_trouble or ctx.result_data("survey").get("survey", {}).get("internet") is False:
                    hint_key = "packages.failed_hint_no_internet"
                else:
                    hint_key = "packages.failed_hint"
                log.warning("PACKAGES_FAILED pip_exit=%d network_trouble=%s hint=%s", code, network_trouble, hint_key)
                return StageResult.fail(ctx.t("packages.failed"), error_code="E-SYS-002", detail=f"pip exit {code}", hint=ctx.t(hint_key))
        if total:
            ctx.console.progress(label, total, total)
            ctx.console.progress_end()

        missing = verify_imports(python)
        if missing:
            return StageResult.fail(ctx.t("packages.verify_failed", names=", ".join(missing)), error_code="E-SYS-002", hint=ctx.t("packages.failed_hint"))
        log.info("PACKAGES_INSTALLED count=%d hash_verified=%s offline=%s", len(installed), has_lock, bool(wheels))
        data = {"python": str(python), "hash_verified": has_lock, "installed": installed[:200], "inputs_key": inputs}
        if not has_lock:
            return StageResult.warn(ctx.t("packages.ok_unpinned", count=len(installed)), error_code="E-SYS-041", hint=ctx.t("packages.unpinned_hint"), **data)
        return StageResult.ok(ctx.t("packages.ok", count=len(installed) or total), **data)


def register(options: Options) -> Iterator[PythonCheckStage | VenvStage | PackagesStage]:
    """Factory used by ``build_default_pipeline`` (three stages in order)."""
    yield PythonCheckStage()
    yield VenvStage()
    yield PackagesStage()
