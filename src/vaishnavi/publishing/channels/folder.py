"""FolderChannel — copy the document into a synced folder (OneDrive / Google Drive), ``YYYY-MM`` sub-folder."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from pathlib import Path

from vaishnavi.core.clock import today_ist
from vaishnavi.core.errors import PublishError

log = logging.getLogger("vaishnavi.publishing.channels.folder")


class FolderChannel:
    """The simplest route: the phone's cloud-drive app shows the file."""

    name = "folder"

    def __init__(self, root: Path, *, must_exist: bool = False) -> None:
        self._root = Path(root)
        self._must_exist = must_exist

    def send(self, path: Path, *, summary: Sequence[str], subject: str) -> None:
        """Copy into ``<root>/<YYYY-MM>/``; a missing drive is a configuration problem (E-PUB-001)."""
        if not str(self._root).strip():
            raise PublishError("E-PUB-001", channel=self.name, detail="no folder configured")
        if self._must_exist and not self._root.exists():
            raise PublishError("E-PUB-001", channel=self.name, detail=f"folder not found: {self._root}")
        target_dir = self._root / today_ist().strftime("%Y-%m")
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            shutil.copy2(path, target)
            (target_dir / (path.stem + ".txt")).write_text("\n".join(summary) + "\n", encoding="utf-8")
        except OSError as exc:
            raise PublishError("E-PUB-003", channel=self.name, detail=f"{type(exc).__name__}: {exc}") from exc
        log.info("PUBLISH_FOLDER file=%s dir=%s", path.name, target_dir)
