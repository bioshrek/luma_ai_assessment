"""LeRobot v3.0 adapter — sources A (`pusht`) and B (`aloha_sim_insertion_human`).

v3.0 concatenates every episode of a dataset into a few shared parquet files; episode
boundaries live in `meta/episodes/**` as `[dataset_from_index, dataset_to_index)` row ranges
(ADR 002). So "fetch one episode" means: download the shared shard once, then slice.

Nothing upstream is trusted for meaning: `info.json` calls pusht's two channels `motor_0` and
`motor_1` under a `motors` key, and they are neither. Semantics come from
`config/embodiments.yaml`, applied positionally after asserting the vector width.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.typing import NDArray

from rdp.application.ports import EpisodeRef, RawEpisode
from rdp.domain.action_spec import Channel, ChannelRole, SignalOrigin
from rdp.domain.boundary import EndReason, EpisodeBoundary, SuccessAdjudicator, TerminationSource
from rdp.domain.camera import CameraEncoding, CameraMount, CameraSpec
from rdp.domain.capabilities import Capabilities
from rdp.domain.embodiment import Embodiment, EmbodimentRegistry
from rdp.domain.episode import CanonicalEpisode, EpisodeMeta, make_uid
from rdp.domain.errors import InvariantViolation
from rdp.domain.frames import FrameTable
from rdp.domain.provenance import REAL_TIMESTAMP, Provenance, synthesized_at
from rdp.domain.source import Source
from rdp.infrastructure.sources.staging import is_staged, mark_staged
from rdp.infrastructure.sources.upstream_fetch import UpstreamFetcher, UpstreamNotFound
from rdp.infrastructure.storage.atomic_fs import atomic_write, atomic_write_text

ADAPTER_VERSION = "lerobot@1.2.0"

INFO_PATH = "meta/info.json"
TASKS_PATH = "meta/tasks.parquet"
EPISODES_DIR = "meta/episodes"

ACTION_COLUMN = "action"
STATE_COLUMN = "observation.state"
TIMESTAMP_COLUMN = "timestamp"
FRAME_INDEX_COLUMN = "frame_index"
DONE_COLUMN = "next.done"
"""LeRobot's end-of-episode marker. ADR 002: it merges terminated and truncated into one flag."""
_INDEX_COLUMN = "index"
"""The dataset-global row id `meta/episodes` boundaries are expressed in."""

# Upstream bookkeeping: row ids into the concatenated dataset, not episode content. Dropping
# them is a lossy transform and is recorded as such in Provenance.transforms.
DROPPED_COLUMNS = ("episode_index", "frame_index", "index", "task_index")

ROWS_FILE = "rows.parquet"
REF_FILE = "ref.json"

_MAX_META_FILES = 64


class LeRobotAdapter:
    """Implements `SourcePort` for any LeRobot v3.0 dataset."""

    def __init__(self, fetcher: UpstreamFetcher, embodiments: EmbodimentRegistry) -> None:
        self._fetcher = fetcher
        self._embodiments = embodiments

    @property
    def kind(self) -> str:
        return "lerobot"

    @property
    def adapter_version(self) -> str:
        return ADAPTER_VERSION

    # -- discovery ---------------------------------------------------------------------

    def list_episodes(self, source: Source) -> Iterator[EpisodeRef]:
        info = self._info(source)
        tasks = self._tasks(source)
        for row in self._episode_rows(source, int(info["total_episodes"])):
            episode_index = int(row["episode_index"])
            yield EpisodeRef(
                source_id=source.source_id,
                upstream_id=f"episode_{episode_index:06d}",
                n_frames_hint=int(row["length"]),
                extra={
                    "episode_index": episode_index,
                    "data_chunk_index": int(row["data/chunk_index"]),
                    "data_file_index": int(row["data/file_index"]),
                    "dataset_from_index": int(row["dataset_from_index"]),
                    "dataset_to_index": int(row["dataset_to_index"]),
                    "tasks": list(row.get("tasks") or []),
                    "all_tasks": tasks,
                },
            )

    def _episode_rows(self, source: Source, total_episodes: int) -> Iterator[dict[str, Any]]:
        """`info.json` gives no index of the episode-metadata files, so probe by convention."""
        seen = 0
        for file_index in range(_MAX_META_FILES):
            rel = f"{EPISODES_DIR}/chunk-000/file-{file_index:03d}.parquet"
            try:
                path = self._fetcher.local_path(source, rel)
            except UpstreamNotFound:
                break
            table = pq.read_table(path)
            for row in table.to_pylist():
                yield row
                seen += 1
            if seen >= total_episodes:
                return
        if seen != total_episodes:
            raise InvariantViolation(
                f"{source.source_id}: found {seen} episode metadata rows, "
                f"info.json declares {total_episodes}"
            )

    # -- fetch -------------------------------------------------------------------------

    def fetch(self, ref: EpisodeRef, source: Source, dest: Path) -> RawEpisode:
        if is_staged(dest, self.adapter_version):
            return RawEpisode(ref=ref, path=dest, upstream_revision=source.revision)

        info = self._info(source)
        data_rel = str(info["data_path"]).format(
            chunk_index=ref.extra["data_chunk_index"], file_index=ref.extra["data_file_index"]
        )
        shard = self._fetcher.local_path(source, data_rel)
        start = int(ref.extra["dataset_from_index"])
        stop = int(ref.extra["dataset_to_index"])
        # `dataset_from_index` counts rows of the WHOLE dataset while the shard holds only its
        # own file's rows, so it is a slice position for `file-000` and a lie for every other
        # file. The rows are selected by the dataset-global `index` column they refer to.
        table = pq.read_table(shard)
        index = table.column(_INDEX_COLUMN).to_numpy(zero_copy_only=False)
        rows = table.filter(pa.array((index >= start) & (index < stop)))
        if rows.num_rows != stop - start:
            raise InvariantViolation(
                f"{ref.upstream_id}: {data_rel} holds {rows.num_rows} of the "
                f"{stop - start} rows the episode metadata claims"
            )

        atomic_write(dest / ROWS_FILE, lambda tmp: pq.write_table(rows, tmp))
        atomic_write_text(
            dest / REF_FILE,
            json.dumps(
                {
                    "source_id": ref.source_id,
                    "upstream_id": ref.upstream_id,
                    "extra": dict(ref.extra),
                    # Carried along so `normalize` needs no network and no second read.
                    "info": info,
                    "revision": source.revision,
                    "data_path": data_rel,
                },
                indent=2,
                sort_keys=True,
            ),
        )
        # The marker is written last: its presence means everything before it is durable.
        mark_staged(dest, self.adapter_version, rows=rows.num_rows)
        return RawEpisode(ref=ref, path=dest, upstream_revision=source.revision)

    # -- normalize ---------------------------------------------------------------------

    def normalize(self, raw: RawEpisode, source: Source) -> CanonicalEpisode:
        staged = json.loads((raw.path / REF_FILE).read_text())
        info = staged["info"]
        table = pq.read_table(raw.path / ROWS_FILE)
        embodiment = self._embodiments.get(source.embodiment)

        action = _vector_column(table, ACTION_COLUMN)
        state = _vector_column(table, STATE_COLUMN)
        embodiment.assert_width(
            action_dim=action.shape[1] if action is not None else 0,
            state_dim=state.shape[1] if state is not None else 0,
        )

        timestamp = np.asarray(table.column(TIMESTAMP_COLUMN).to_numpy(zero_copy_only=False))
        if timestamp.size:
            t = (timestamp - timestamp[0]).astype(np.float64)
        else:
            t = np.zeros(0, dtype=np.float64)
        columns: dict[str, NDArray[Any]] = {"t": t}
        columns.update(_named(embodiment.action_channels, "action", action))
        columns.update(_named(embodiment.state_channels, "state", state))

        raw_columns: list[str] = []
        for name in table.column_names:
            if name in DROPPED_COLUMNS or name in (ACTION_COLUMN, STATE_COLUMN, TIMESTAMP_COLUMN):
                continue
            values = np.asarray(table.column(name).to_numpy(zero_copy_only=False))
            if values.ndim != 1:
                continue
            # Unmodeled upstream data is preserved, never dropped (design §2.4).
            columns[f"raw.{name}"] = values
            raw_columns.append(f"raw.{name}")

        frames = FrameTable(columns=columns, raw_frame_columns=tuple(raw_columns))
        meta = self._meta(staged, info, table, frames, source, embodiment, timestamp)
        return CanonicalEpisode(meta=meta, frames=frames)

    def _meta(
        self,
        staged: dict[str, Any],
        info: dict[str, Any],
        table: pa.Table,
        frames: FrameTable,
        source: Source,
        embodiment: Embodiment,
        timestamp: NDArray[Any],
    ) -> EpisodeMeta:
        extra = staged["extra"]
        fps_nominal = float(info["fps"])
        present = set(table.column_names)
        tasks = [str(task) for task in extra.get("tasks", [])]

        cameras = _cameras(info, source.with_video)
        capabilities = Capabilities(
            has_action=bool(embodiment.action_channels),
            has_state=bool(embodiment.state_channels),
            has_gripper=any(c.role is ChannelRole.GRIPPER for c in embodiment.action_channels),
            has_rgb=bool(cameras),
            # Only a decodable local file counts; a declared mp4 we never downloaded does not.
            has_video=bool(cameras) and source.with_video,
            has_language=bool(tasks),
            has_reward="next.reward" in present,
            has_termination_signal=DONE_COLUMN in present,
            is_real_robot=embodiment.is_real_robot,
            is_teleop=embodiment.is_teleop,
        )

        return EpisodeMeta(
            uid=make_uid(source.source_id, staged["upstream_id"]),
            source_id=source.source_id,
            upstream_id=staged["upstream_id"],
            embodiment=embodiment.embodiment_id,
            task=tasks[0] if len(tasks) == 1 else None,
            n_frames=frames.n_frames,
            action_spec=embodiment.action_spec(),
            state_spec=embodiment.state_spec(),
            capabilities=capabilities,
            cameras=cameras,
            provenance=_provenance(table, staged, fps_nominal, timestamp),
            boundary=_boundary(table),
            fps_nominal=fps_nominal,
            fps_effective=_effective_fps(frames.t, fps_nominal),
            duration_s=float(frames.t[-1] - frames.t[0]) if frames.n_frames > 1 else 0.0,
            # Named, not guessed: `TERMINATION_CONSISTENCY` must read the column upstream
            # actually wrote, and only if it survived into the frame table.
            termination_column=(
                f"raw.{DONE_COLUMN}"
                if f"raw.{DONE_COLUMN}" in frames.raw_frame_columns
                else None
            ),
            raw_extra={
                "lerobot": {
                    "codebase_version": info.get("codebase_version"),
                    "robot_type": info.get("robot_type"),
                    "episode_index": extra.get("episode_index"),
                    "dataset_from_index": extra.get("dataset_from_index"),
                    "dataset_to_index": extra.get("dataset_to_index"),
                    "tasks": tasks,
                }
            },
            raw_frame_columns=frames.raw_frame_columns,
        )

    # -- upstream metadata ---------------------------------------------------------------

    def _info(self, source: Source) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(
            self._fetcher.local_path(source, INFO_PATH).read_text()
        )
        return payload

    def _tasks(self, source: Source) -> list[str]:
        try:
            path = self._fetcher.local_path(source, TASKS_PATH)
        except UpstreamNotFound:
            return []
        table = pq.read_table(path)
        # The task string sits in the index column, not in a nicely named one.
        column = "__index_level_0__" if "__index_level_0__" in table.column_names else "task"
        if column not in table.column_names:
            return []
        return [str(value) for value in table.column(column).to_pylist()]


def _vector_column(table: pa.Table, name: str) -> NDArray[Any] | None:
    """Read a per-frame vector. pusht stores `fixed_size_list`, aloha a plain `list`."""
    if name not in table.column_names:
        return None
    values = table.column(name).to_pylist()
    return np.asarray(values, dtype=np.float64)


def _named(
    channels: tuple[Channel, ...], prefix: str, values: NDArray[Any] | None
) -> dict[str, NDArray[Any]]:
    if values is None or not channels:
        return {}
    return {f"{prefix}.{channel.name}": values[:, i] for i, channel in enumerate(channels)}


def _cameras(info: dict[str, Any], with_video: bool) -> tuple[CameraSpec, ...]:
    cameras = []
    for name, feature in info.get("features", {}).items():
        if feature.get("dtype") not in ("video", "image"):
            continue
        shape = feature.get("shape", [0, 0, 0])
        cameras.append(
            CameraSpec(
                name=name,
                # Upstream never states where the camera is; a plausible guess would be worse
                # than an honest 'unknown'. Only an explicit name hint is trusted.
                mount=CameraMount.WRIST if "wrist" in name else CameraMount.UNKNOWN,
                resolution=(int(shape[0]), int(shape[1])),
                channels=int(shape[2]) if len(shape) > 2 else 0,
                encoding=CameraEncoding.MP4_SIDECAR,
                is_present=with_video,
            )
        )
    return tuple(cameras)


def _provenance(
    table: pa.Table,
    staged: dict[str, Any],
    fps: float,
    timestamp: NDArray[Any],
) -> Provenance:
    return Provenance(
        is_original=True,
        timestamp_source=_timestamp_source(table, fps, timestamp),
        frame_index_source="upstream",
        signal_origin={"action": SignalOrigin.MEASURED, "state": SignalOrigin.MEASURED},
        transforms=(
            {
                "op": "drop_columns",
                "columns": [c for c in DROPPED_COLUMNS if c in table.column_names],
                "reason": "row ids into the concatenated dataset; not episode content",
            },
        ),
        upstream_revision=staged["revision"],
        adapter_version=ADAPTER_VERSION,
    )


def _timestamp_source(table: pa.Table, fps: float, timestamp: NDArray[Any]) -> str:
    """Decide by measurement, not by documentation.

    If `timestamp` reproduces `frame_index / fps` exactly in float32, the column is not a clock
    reading at all — it is the frame counter restated. Calling that `real` would let the
    timestamp QC rules pass by construction.
    """
    if FRAME_INDEX_COLUMN not in table.column_names or timestamp.size == 0:
        return REAL_TIMESTAMP
    frame_index = np.asarray(table.column(FRAME_INDEX_COLUMN).to_numpy(zero_copy_only=False))
    synthetic = (frame_index / fps).astype(np.float32)
    if np.array_equal(synthetic, timestamp.astype(np.float32)):
        return synthesized_at(fps)
    return REAL_TIMESTAMP


def _boundary(table: pa.Table) -> EpisodeBoundary:
    present = set(table.column_names)
    if "next.success" not in present:
        # No adjudicator exists in this dataset — which is not the same as "it failed".
        return EpisodeBoundary(
            termination_source=TerminationSource.ENV_RULE,
            end_reason=EndReason.UNKNOWN,
            is_truncated=None,
            success=None,
            success_adjudicator=SuccessAdjudicator.NONE,
        )
    success = bool(np.any(table.column("next.success").to_numpy(zero_copy_only=False)))
    return EpisodeBoundary(
        termination_source=TerminationSource.ENV_RULE,
        end_reason=EndReason.SUCCESS if success else EndReason.UNKNOWN,
        # ADR 002: LeRobot merges terminated and truncated into one `done` flag, so which one
        # happened is not recoverable. NULL, not a guess.
        is_truncated=None,
        success=success,
        success_adjudicator=SuccessAdjudicator.SIMULATOR,
    )


def _effective_fps(t: NDArray[Any], fallback: float) -> float:
    if t.size < 2:
        return fallback
    median_dt = float(np.median(np.diff(t)))
    return 1.0 / median_dt if median_dt > 0 else fallback
