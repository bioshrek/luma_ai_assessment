"""The normalized store: one directory per episode, `frames.parquet` + `episode.json`.

Columnar because every consumer reads a few channels over many rows. Per episode rather than
one giant table because an episode is the unit of ingestion, resume and export — and because
re-normalizing one episode must never rewrite a shared file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.typing import NDArray

from rdp.domain.episode import CanonicalEpisode, EpisodeMeta
from rdp.domain.frames import FrameTable
from rdp.infrastructure.storage.atomic_fs import atomic_write, atomic_write_text

FRAMES_FILE = "frames.parquet"
META_FILE = "episode.json"
_RAW_COLUMNS_KEY = b"rdp.raw_frame_columns"


class ParquetFrameStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, episode: CanonicalEpisode) -> str:
        meta = episode.meta
        directory = self.root / meta.source_id / _safe(meta.upstream_id)
        table = _to_table(episode.frames)
        atomic_write(directory / FRAMES_FILE, lambda tmp: pq.write_table(table, tmp))
        atomic_write_text(
            directory / META_FILE,
            json.dumps(meta.model_dump(mode="json"), indent=2, sort_keys=True),
        )
        return str(directory.relative_to(self.root))

    def read_frames(self, path: str) -> FrameTable:
        table = pq.read_table(self.resolve(path) / FRAMES_FILE)
        raw_columns: tuple[str, ...] = ()
        metadata = table.schema.metadata or {}
        if _RAW_COLUMNS_KEY in metadata:
            raw_columns = tuple(json.loads(metadata[_RAW_COLUMNS_KEY].decode()))
        columns = {name: _to_numpy(table.column(name)) for name in table.column_names}
        return FrameTable(columns=columns, raw_frame_columns=raw_columns)

    def read_meta(self, path: str) -> EpisodeMeta:
        payload = json.loads((self.resolve(path) / META_FILE).read_text())
        return EpisodeMeta.model_validate(payload)

    def resolve(self, path: str) -> Path:
        """`episodes.frames_path` is stored relative to the store root, so the root can move."""
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.root / candidate


def _safe(upstream_id: str) -> str:
    """Upstream ids are not path-safe (RLDS uses slashes, EPIC uses colons)."""
    return upstream_id.replace("/", "__").replace(":", "_")


def _to_table(frames: FrameTable) -> pa.Table:
    arrays = [pa.array(frames.column(name)) for name in frames.column_names]
    table = pa.Table.from_arrays(arrays, names=list(frames.column_names))
    return table.replace_schema_metadata(
        {_RAW_COLUMNS_KEY: json.dumps(list(frames.raw_frame_columns)).encode()}
    )


def _to_numpy(column: pa.ChunkedArray) -> NDArray[Any]:
    return np.asarray(column.to_numpy(zero_copy_only=False))
