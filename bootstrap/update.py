"""``main.py --update <zip-or-folder>`` — apply a new release safely (docs/13 Phase 7, docs/17 §7).

``plan_update`` validates the release (``pyproject.toml`` + ``src/vaishnavi`` + ``bootstrap`` + ``main.py``),
unpacking a ZIP first; ``apply_update`` keeps the current program files in ``.previous/``, copies the new
ones in, runs the two checks the caller supplies (``install`` = pip from the lock file, ``selftest`` =
``python -m vaishnavi selftest``) and **rolls back** when either fails. The database is never touched here —
the mode handler takes a ``pre-update`` snapshot before calling this module.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

log = logging.getLogger("bootstrap.update")

REQUIRED: Final = ("pyproject.toml", "src", "bootstrap", "main.py")
OPTIONAL: Final = ("docs", "config/festivals.toml", "requirements.lock", "README.md", "CHANGELOG.md")
PREVIOUS_DIR: Final = ".previous"
_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)

CheckRunner = Callable[[str], tuple[bool, str]]
"""``run_check("install" | "selftest") -> (ok, detail)``."""


class UpdateError(Exception):
    """The release cannot be applied (reported in plain words, E-SYS-060)."""


@dataclass(frozen=True)
class Entry:
    """One top-level item to replace."""

    name: str
    is_dir: bool


@dataclass(frozen=True)
class UpdatePlan:
    """Validated release."""

    program_dir: Path
    source: Path
    current_version: str
    new_version: str
    entries: list[Entry] = field(default_factory=list)


@dataclass(frozen=True)
class UpdateResult:
    """Outcome."""

    ok: bool
    detail: str = ""
    rolled_back: bool = False


def _version_of(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace") if (root / "pyproject.toml").exists() else ""
    m = _VERSION_RE.search(text)
    return m.group(1) if m else "?"


def _unpack(archive: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="vsm-update-"))
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            if member.startswith(("/", "\\")) or ".." in Path(member).parts:
                raise UpdateError(f"unsafe path in archive: {member}")
        zf.extractall(tmp)
    children = [p for p in tmp.iterdir() if p.is_dir()]
    # a zip of one top folder (release-1.0.0/...) → use that folder
    if len(children) == 1 and not (tmp / "pyproject.toml").exists() and (children[0] / "pyproject.toml").exists():
        return children[0]
    return tmp


def plan_update(program_dir: Path, source: Path) -> UpdatePlan:
    """Validate ``source`` (folder or ZIP) as a release of this program."""
    program_dir = Path(program_dir)
    source = Path(source)
    if not source.exists():
        raise UpdateError(f"release not found: {source}")
    if source.is_file():
        if source.suffix.lower() != ".zip":
            raise UpdateError("a release is a ZIP file or an unpacked folder")
        source = _unpack(source)
    missing = [name for name in REQUIRED if not (source / name).exists()]
    if missing:
        raise UpdateError("not a Vaishnavi release — missing " + ", ".join(missing))
    if not (source / "src" / "vaishnavi" / "__init__.py").exists():
        raise UpdateError("not a Vaishnavi release — src/vaishnavi is incomplete")
    entries = [Entry(name, (source / name).is_dir()) for name in (*REQUIRED, *OPTIONAL) if (source / name).exists()]
    plan = UpdatePlan(program_dir, source, _version_of(program_dir), _version_of(source), entries)
    log.info("UPDATE_PLANNED from=%s to=%s entries=%s", plan.current_version, plan.new_version, ",".join(e.name for e in entries))
    return plan


def _replace(dst: Path, src: Path) -> None:
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        shutil.copy2(src, dst)


def apply_update(plan: UpdatePlan, *, run_check: CheckRunner) -> UpdateResult:
    """Stage the old files, copy the new ones, run checks, roll back on failure."""
    previous = plan.program_dir / PREVIOUS_DIR
    if previous.exists():
        shutil.rmtree(previous)
    previous.mkdir()
    staged: list[Entry] = []
    try:
        for e in plan.entries:
            current = plan.program_dir / e.name
            if current.exists():
                _replace(previous / e.name, current)
                staged.append(e)
            _replace(current, plan.source / e.name)
        log.info("UPDATE_FILES_REPLACED entries=%d previous=%s", len(plan.entries), previous)
        for stage in ("install", "selftest"):
            ok, detail = run_check(stage)
            log.info("UPDATE_CHECK stage=%s ok=%s detail=%s", stage, ok, detail[:200])
            if not ok:
                _rollback(plan, previous, staged)
                return UpdateResult(False, f"{stage} failed: {detail}", rolled_back=True)
    except Exception as exc:  # noqa: BLE001 - any copy failure must leave the old program in place
        log.exception("UPDATE_FAILED error=%r", exc)
        _rollback(plan, previous, staged)
        return UpdateResult(False, f"{type(exc).__name__}: {exc}", rolled_back=True)
    log.info("UPDATE_APPLIED version=%s (previous kept in %s)", plan.new_version, previous)
    return UpdateResult(True, f"updated {plan.current_version} → {plan.new_version}")


def _rollback(plan: UpdatePlan, previous: Path, staged: list[Entry]) -> None:
    for e in plan.entries:
        current = plan.program_dir / e.name
        if current.exists():
            shutil.rmtree(current) if current.is_dir() else current.unlink()
    for e in staged:
        _replace(plan.program_dir / e.name, previous / e.name)
    log.warning("UPDATE_ROLLED_BACK to=%s", plan.current_version)
