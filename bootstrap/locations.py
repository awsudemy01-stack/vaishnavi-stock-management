"""Primary (live data) and secondary (backup) locations: ask, validate, record (stdlib only).

Rules (docs/14 §4, ADR-032): the two folders must be writable, have free space, must not be nested
in each other, and *should* be on different physical devices — a same-device backup is accepted
only after an explicit warning (it protects against mistakes, not against disk failure). The result
is written to ``config/locations.toml``; the application and the doctor read it from there.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bootstrap.stages import StageContext, StageResult
from bootstrap.wizard import Kind, Question

if TYPE_CHECKING:
    from bootstrap.cli import Options

log = logging.getLogger("bootstrap.locations")

LOCATIONS_FILE = "locations.toml"
DEFAULT_PRIMARY_NAME = "VaishnaviStore"
DEFAULT_SECONDARY_NAME = "VaishnaviStore-Backups"
MIN_FREE_PRIMARY_GIB = 2.0
MIN_FREE_SECONDARY_GIB = 5.0
GIB = 1024**3


# ----------------------------------------------------------------------------- probes
def device_id(path: Path) -> str:
    """Identifier of the physical volume holding ``path`` (nearest existing ancestor)."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if sys.platform.startswith("win"):
        root = Path(probe.resolve().anchor)
        serial = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(str(root)), None, 0, ctypes.byref(serial), None, None, None, 0)  # type: ignore[attr-defined]
        return f"{root}:{serial.value:08X}" if ok else str(root)
    try:
        return str(os.stat(probe).st_dev)
    except OSError:
        return str(probe.anchor)


def free_gib(path: Path) -> float | None:
    """Free space (GiB) on the volume of ``path`` (nearest existing ancestor)."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return round(shutil.disk_usage(probe).free / GIB, 1)
    except OSError:
        return None


def ensure_writable(path: Path) -> str | None:
    """Create ``path`` if needed and prove we can write in it; returns a problem text or None."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{uuid.uuid4().hex[:8]}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return str(exc)
    return None


def verify_existing(path: Path) -> str | None:
    """For a location chosen earlier: it must **already exist** and be writable.

    Never creates it — an unplugged USB drive or a missing mount would otherwise be silently replaced
    by an empty folder on the internal disk, and the owner would believe the data/backups are there.
    """
    if not path.is_dir():
        return f"folder does not exist: {path}"
    try:
        probe = path / f".write-test-{uuid.uuid4().hex[:8]}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return str(exc)
    return None


def is_nested(a: Path, b: Path) -> bool:
    """True when one folder is inside the other (or they are the same)."""
    ra, rb = a.resolve(), b.resolve()
    return ra == rb or ra in rb.parents or rb in ra.parents


def is_removable_or_other_drive(path: Path, home: Path) -> bool:
    """Heuristic 'is this a different disk than the home folder'."""
    return device_id(path) != device_id(home)


def suggest_secondary(home: Path, survey: dict) -> Path:
    """Best default for the backup folder: another drive if visible, else a sibling folder."""
    if sys.platform.startswith("win"):
        for letter in "DEFGH":
            root = Path(f"{letter}:\\")
            if root.exists() and device_id(root) != device_id(home):
                return root / DEFAULT_SECONDARY_NAME
    else:
        for base in ("/media", "/Volumes", "/mnt", f"/run/media/{os.environ.get('USER', '')}"):
            b = Path(base)
            if b.is_dir():
                for mount in sorted(p for p in b.iterdir() if p.is_dir()):
                    try:
                        if device_id(mount) != device_id(home) and os.access(mount, os.W_OK):
                            return mount / DEFAULT_SECONDARY_NAME
                    except OSError:
                        continue
    return home / DEFAULT_SECONDARY_NAME


# ----------------------------------------------------------------------------- persistence
@dataclass(frozen=True)
class Locations:
    """Chosen locations."""

    primary: Path
    secondary: Path
    same_device: bool

    def write(self, path: Path) -> None:
        """Write ``config/locations.toml`` (atomic)."""
        text = (
            "# Where Vaishnavi Store Manager keeps its data. Chosen during installation; change with care\n"
            "# (docs/14-database-backup-and-recovery.md).\n\n"
            "[locations]\n"
            f'primary = "{_toml(self.primary)}"      # live database, receipts, images, logs\n'
            f'secondary = "{_toml(self.secondary)}"  # backup snapshots (keep on another disk / USB / synced folder)\n'
            f"same_device = {'true' if self.same_device else 'false'}\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        log.info("LOCATIONS_WRITTEN primary=%s secondary=%s same_device=%s", self.primary, self.secondary, self.same_device)


def _toml(p: Path) -> str:
    return str(p).replace("\\", "\\\\").replace('"', '\\"')


def read_locations(path: Path) -> Locations | None:
    """Parse an existing ``locations.toml`` (None when missing or unreadable)."""
    import tomllib  # noqa: PLC0415

    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")).get("locations", {})
        return Locations(Path(data["primary"]), Path(data["secondary"]), bool(data.get("same_device", False)))
    except (OSError, KeyError, ValueError, TypeError) as exc:
        log.warning("locations.toml unreadable: %s", exc)
        return None


# ----------------------------------------------------------------------------- stage
@dataclass
class LocationsStage:
    """Stage 8: where does the data live, where do backups go."""

    key: str = "locations"
    title_key: str = "stage.locations"

    def applies(self, ctx: StageContext) -> bool:
        """Always."""
        return True

    def cache_key(self, ctx: StageContext) -> str | None:
        """Not cached — existence/writability of both folders is re-verified on every start."""
        return None

    def run(self, ctx: StageContext) -> StageResult:
        """Reuse ``locations.toml`` when valid; otherwise ask, validate, write."""
        assert ctx.wizard is not None
        cfg_path = ctx.paths.config_dir / LOCATIONS_FILE
        existing = read_locations(cfg_path)
        if existing is not None:
            primary_problem = verify_existing(existing.primary)
            secondary_problem = verify_existing(existing.secondary)
            if primary_problem is None and secondary_problem is None:
                same_now = device_id(existing.primary) == device_id(existing.secondary)
                if same_now != existing.same_device:
                    log.warning("LOCATIONS_DEVICE_CHANGED recorded_same_device=%s now=%s", existing.same_device, same_now)
                log.info("LOCATIONS_REUSED primary=%s secondary=%s same_device=%s", existing.primary, existing.secondary, same_now)
                msg = ctx.t("loc.reused", primary=str(existing.primary), secondary=str(existing.secondary))
                if same_now:
                    return StageResult.warn(msg, error_code="E-SYS-012", hint=ctx.t("loc.same_device_hint"), primary=str(existing.primary), secondary=str(existing.secondary), same_device=True)
                return StageResult.ok(msg, primary=str(existing.primary), secondary=str(existing.secondary), same_device=False)
            if primary_problem is not None:
                # Primary unusable → stop; this is the live database folder.
                return StageResult.fail(ctx.t("loc.primary_unusable", path=str(existing.primary)), error_code="E-SYS-013", hint=ctx.t("loc.primary_unusable_hint"), detail=primary_problem)
            # Secondary (e.g. unplugged USB) → WARN; backups queue until it returns (docs/14 §5).
            log.warning("LOCATIONS_SECONDARY_MISSING path=%s detail=%s", existing.secondary, secondary_problem)
            return StageResult.warn(ctx.t("loc.secondary_missing", path=str(existing.secondary)), error_code="E-SYS-014", hint=ctx.t("loc.secondary_missing_hint"), detail=secondary_problem or "", primary=str(existing.primary), secondary=str(existing.secondary), same_device=existing.same_device)

        home = Path.home()
        survey = ctx.result_data("survey").get("survey", {})
        ctx.console.info(ctx.t("loc.intro"))

        primary = self._ask_folder(ctx, "primary_location", "loc.ask_primary", "loc.help_primary", home / DEFAULT_PRIMARY_NAME, MIN_FREE_PRIMARY_GIB)
        previous: Path | None = None
        while True:
            secondary = self._ask_folder(ctx, "secondary_location", "loc.ask_secondary", "loc.help_secondary", suggest_secondary(home, survey), MIN_FREE_SECONDARY_GIB, force=previous is not None)
            repeated = previous is not None and secondary == previous
            previous = secondary
            if is_nested(primary, secondary):
                ctx.console.warn(ctx.t("loc.err_nested"))
                if repeated or ctx.options.assume_yes:
                    secondary = primary.parent / DEFAULT_SECONDARY_NAME  # last-resort sibling folder
                    if ensure_writable(secondary) or is_nested(primary, secondary):
                        return StageResult.fail(ctx.t("loc.err_nested"), error_code="E-SYS-013", hint=ctx.t("loc.primary_unusable_hint"))
                else:
                    continue
            same = device_id(primary) == device_id(secondary)
            if same:
                ctx.console.warn(ctx.t("loc.same_device_warn"))
                # Unattended (--yes) or the owner keeps giving the same answer: accept with a WARN
                # instead of looping; the Settings screen lets them move backups later.
                if not (ctx.options.assume_yes or repeated):
                    accept = ctx.wizard.ask(Question(key="accept_same_device", kind=Kind.YESNO, prompt_key="loc.same_device_ask", default="no", allow_back=False, persist=False))
                    if accept != "yes":
                        continue
            break

        locs = Locations(primary, secondary, same)
        locs.write(cfg_path)
        data = {"primary": str(primary), "secondary": str(secondary), "same_device": same, "free_primary_gib": free_gib(primary), "free_secondary_gib": free_gib(secondary)}
        msg = ctx.t("loc.chosen", primary=str(primary), secondary=str(secondary), free_p=data["free_primary_gib"], free_s=data["free_secondary_gib"])
        if same:
            return StageResult.warn(msg, error_code="E-SYS-012", hint=ctx.t("loc.same_device_hint"), **data)
        return StageResult.ok(msg, **data)

    def _ask_folder(self, ctx: StageContext, key: str, prompt_key: str, help_key: str, default: Path, min_free: float, *, force: bool = False) -> Path:
        assert ctx.wizard is not None

        def validate(value: str) -> str | None:
            p = Path(value)
            if not p.is_absolute():
                return ctx.t("loc.err_relative")
            problem = ensure_writable(p)
            if problem:
                return ctx.t("loc.err_not_writable", path=str(p), detail=problem)
            free = free_gib(p)
            if free is not None and free < min_free:
                return ctx.t("loc.err_space", path=str(p), free=free, need=min_free)
            return None

        answer = ctx.wizard.ask(Question(key=key, kind=Kind.PATH, prompt_key=prompt_key, help_key=help_key, default=str(default), validate=validate, allow_back=False), force=force)
        return Path(answer)


def register(options: Options) -> LocationsStage:
    """Factory used by ``build_default_pipeline``."""
    return LocationsStage()
