"""The normalized store: one directory per episode, `frames.parquet` + `episode.json`.

Columnar because every consumer reads a few channels over many rows. Per episode rather than
one giant table because an episode is the unit of ingestion, resume and export — and because
re-normalizing one episode must never rewrite a shared file.
"""

from __future__ import annotations

import json
from functools import partial
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
STREAMS_DIR = "streams"
_RAW_COLUMNS_KEY = b"rdp.raw_frame_columns"


class ParquetFrameStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, episode: CanonicalEpisode) -> str:
        meta = episode.meta
        directory = self.root / meta.source_id / _safe(meta.upstream_id)
        table = _to_table(episode.frames)
        atomic_write(directory / FRAMES_FILE, lambda tmp: pq.write_table(table, tmp))
        for stream_id, stream in episode.streams.items():
            # A separate file, not extra columns: the IMU runs at ~198 Hz against a 50 fps video,
            # and joining them into one table would mean resampling one of them (invariant 17).
            atomic_write(
                directory / STREAMS_DIR / f"{stream_id}.parquet",
                partial(pq.write_table, _to_table(stream)),
            )
        # A re-normalization may declare fewer streams than the previous one did. `normalized/`
        # is derived data and must end up *equal* to the episode, not merged with its own past:
        # a leftover file would be read back as a stream nobody declared (invariant 2).
        for stale in (directory / STREAMS_DIR).glob("*.parquet"):
            if stale.stem not in episode.streams:
                stale.unlink()
        atomic_write_text(
            directory / META_FILE,
            json.dumps(meta.model_dump(mode="json"), indent=2, sort_keys=True),
        )
        return str(directory.relative_to(self.root))

    def read_frames(self, path: str) -> FrameTable:
        return _read(self.resolve(path) / FRAMES_FILE)

    def read_streams(self, path: str) -> dict[str, FrameTable]:
        directory = self.resolve(path) / STREAMS_DIR
        if not directory.is_dir():
            return {}
        return {file.stem: _read(file) for file in sorted(directory.glob("*.parquet"))}

    def read_meta(self, path: str) -> EpisodeMeta:
        payload = json.loads((self.resolve(path) / META_FILE).read_text())
        return EpisodeMeta.model_validate(payload)

    def resolve(self, path: str) -> Path:
        """`episodes.frames_path` is stored relative to the store root, so the root can move."""
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.root / candidate


def _read(file: Path) -> FrameTable:
    table = pq.read_table(file)
    raw_columns: tuple[str, ...] = ()
    metadata = table.schema.metadata or {}
    if _RAW_COLUMNS_KEY in metadata:
        raw_columns = tuple(json.loads(metadata[_RAW_COLUMNS_KEY].decode()))
    columns = {name: _to_numpy(table.column(name)) for name in table.column_names}
    return FrameTable(columns=columns, raw_frame_columns=raw_columns)


def _safe(upstream_id: str) -> str:
    """Upstream ids are not path-safe (RLDS uses slashes, EPIC uses colons)."""
    return upstream_id.replace("/", "__").replace(":", "_")


def _to_table(frames: FrameTable) -> pa.Table:
    arrays = [_to_arrow(frames.column(name)) for name in frames.column_names]
    table = pa.Table.from_arrays(arrays, names=list(frames.column_names))
    return table.replace_schema_metadata(
        {_RAW_COLUMNS_KEY: json.dumps(list(frames.raw_frame_columns)).encode()}
    )


def _to_arrow(values: NDArray[Any]) -> pa.Array:
    """NaN is stored as a genuine parquet NULL.

    The pipeline never zero-fills and never invents a value, so a NaN in a float column always
    means "upstream did not provide this" — D's unregistered camera-pose frames are the case that
    forces the point. Storing it as NULL makes that legible to any consumer that never reads our
    schema. The round trip is lossless: pyarrow reads a float NULL back as NaN.
    """
    if values.dtype.kind == "f":
        return pa.array(values, mask=np.isnan(values))
    return pa.array(values)


def _to_numpy(column: pa.ChunkedArray) -> NDArray[Any]:
    return np.asarray(column.to_numpy(zero_copy_only=False))
