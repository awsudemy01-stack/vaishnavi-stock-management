"""Doctor check: installed application files match the release manifest (E-SYS-040; stdlib).

``write_manifest(program_dir)`` is run when a release is packaged (and by ``--update``); it records the
SHA-256 of every source file under ``main.py``, ``bootstrap/``, ``src/vaishnavi/`` and ``config/*.toml``
templates — never caches, data, logs or the venv. A missing manifest means a development checkout
(WARN, said plainly); any changed, added or removed file in a release means someone touched the
program → FAIL and the start is refused (docs/13 acceptance).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from vaishnavi import __version__
from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext
from vaishnavi.core.clock import iso_utc
from vaishnavi.platform.paths import program_dir as default_program_dir

log = logging.getLogger("vaishnavi.bootstrap.checks.code_integrity")

MANIFEST_NAME = "MANIFEST.sha256.json"
ROOT_FILES = ("main.py", "pyproject.toml", "requirements.lock")
TREES = ("bootstrap", "src/vaishnavi")
SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp"}


def iter_release_files(program_dir: Path) -> list[Path]:
    """Every file that belongs to the program (relative paths, sorted)."""
    files: list[Path] = []
    for name in ROOT_FILES:
        p = program_dir / name
        if p.is_file():
            files.append(Path(name))
    for tree in TREES:
        base = program_dir / tree
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix in SKIP_SUFFIXES or any(part in SKIP_DIRS for part in p.relative_to(program_dir).parts):
                continue
            files.append(p.relative_to(program_dir))
    return sorted(set(files), key=lambda p: p.as_posix())


def file_sha256(path: Path) -> str:
    """Hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(program_dir: Path) -> Path:
    """Compute and write ``MANIFEST.sha256.json``; returns its path."""
    files = {p.as_posix(): file_sha256(program_dir / p) for p in iter_release_files(program_dir)}
    doc = {"version": __version__, "generated_at": iso_utc(), "algorithm": "sha256", "files": files}
    target = program_dir / MANIFEST_NAME
    target.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    log.info("MANIFEST_WRITTEN files=%d path=%s", len(files), target)
    return target


def verify_manifest(program_dir: Path) -> tuple[list[str], list[str], list[str]] | None:
    """(changed, added, removed) relative paths, or None when there is no manifest."""
    target = program_dir / MANIFEST_NAME
    if not target.exists():
        return None
    try:
        expected: dict[str, str] = json.loads(target.read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError) as exc:
        log.error("MANIFEST_UNREADABLE %s", exc)
        return (["MANIFEST.sha256.json"], [], [])
    actual = {p.as_posix(): file_sha256(program_dir / p) for p in iter_release_files(program_dir)}
    changed = sorted(k for k in expected if k in actual and actual[k] != expected[k])
    added = sorted(k for k in actual if k not in expected)
    removed = sorted(k for k in expected if k not in actual)
    return changed, added, removed


@dataclass
class CodeIntegrityCheck:
    """WARN without a manifest (development checkout) · FAIL E-SYS-040 on any difference."""

    key: str = "code_integrity"
    group: str = "software"

    def run(self, ctx: DoctorContext) -> CheckResult:
        """Compare the installed tree with the release manifest."""
        program_dir = Path(ctx.runtime.get("program_dir") or default_program_dir())
        result = verify_manifest(program_dir)
        if result is None:
            return CheckResult.warn(ctx.t("doctor.code_integrity.no_manifest"), hint=ctx.t("doctor.code_integrity.no_manifest_hint"), detail=str(program_dir), manifest=False)
        changed, added, removed = result
        if changed or added or removed:
            detail = f"changed={changed[:10]} added={added[:10]} removed={removed[:10]}"
            log.error("CODE_INTEGRITY_FAIL %s code=E-SYS-040", detail)
            return CheckResult.fail(ctx.t("doctor.code_integrity.changed", count=len(changed) + len(added) + len(removed)), error_code="E-SYS-040", hint=ctx.t("doctor.code_integrity.changed_hint"), detail=detail, changed=changed, added=added, removed=removed)
        count = len(iter_release_files(program_dir))
        return CheckResult.ok(ctx.t("doctor.code_integrity.ok", count=count), manifest=True, files=count)


CHECKS = [CodeIntegrityCheck()]
