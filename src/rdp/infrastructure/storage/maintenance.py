"""`StoreMaintenance` — the filesystem half of the recovery pass (design §5, iron rule 3).

Deliberately separate from `ParquetFrameStore`: sweeping `raw/` and `cache/` is not the frame
store's business, and the recovery pass needs to see the whole store, not one layer of it.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq

from rdp.infrastructure.persistence.catalog import CATALOG_FILE
from rdp.infrastructure.storage.parquet_frame_store import FRAMES_FILE, ParquetFrameStore

TEMP_SUFFIX = ".tmp"


class StoreMaintenance:
    def __init__(self, root: Path, frame_store: ParquetFrameStore) -> None:
        self.root = root
        self._frames = frame_store

    def sweep_orphan_temp_files(self) -> list[str]:
        """A `*.tmp` is unreferenced by construction — no row ever names one — so it is safe to
        delete unconditionally, and unsafe to keep: the next `atomic_write` would append to it.
        """
        if not self.root.exists():
            return []
        removed = []
        for path in sorted(self.root.rglob(f"*{TEMP_SUFFIX}")):
            if not path.is_file():
                continue
            path.unlink(missing_ok=True)
            removed.append(str(path.relative_to(self.root)))
        return removed

    def frames_readable(self, path: str) -> bool:
        """Open the footer only. The question is "does this file still parse", not "what is in
        it", and a catalog row pointing at rubble must be demoted rather than exported."""
        target = self._frames.resolve(path) / FRAMES_FILE
        try:
            return pq.ParquetFile(target).metadata is not None
        except Exception:
            return False

    def usage_bytes(self) -> dict[str, int]:
        """Bytes on disk per store layer, for the report.

        Reported per layer because the layers are not equally expensive to lose: `raw/` is the
        authoritative copy, while `normalized/` and `cache/` can be deleted and rebuilt.
        """
        usage = {name: _tree_bytes(self.root / name) for name in ("raw", "normalized", "cache")}
        catalog = self.root / CATALOG_FILE
        usage["catalog"] = catalog.stat().st_size if catalog.is_file() else 0
        usage["total"] = sum(usage.values())
        return usage


def _tree_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
