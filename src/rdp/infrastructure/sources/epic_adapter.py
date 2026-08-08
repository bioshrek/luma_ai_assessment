"""EPIC-KITCHENS-100 adapter — source D. The one that is not a robot.

Source D exists in this pipeline to break assumptions the three robot sources let us keep, and
the adapter's shape follows from that (ADR 010, ADR 011):

1. **There is no action.** A human wearing a head camera issues no commands. The only thing
   annotated is *what they were doing*, once, for the whole segment: `(verb, noun)`. So
   `action_spec.level = "episode_label"`, `physical_dim = 0`, and `frames.parquet` has **no**
   action column at all. Every frame-level action rule resolves to
   `SKIPPED(reason=action_level_is_episode_label)` — which is a different, separately counted
   conclusion from "this source has no action".

2. **The layers are independent.** Annotations, EPIC-Fields camera poses and GoPro IMU are three
   separate publications on three separate servers with different video coverage. Each is probed
   per episode; a 404 degrades exactly that layer's capabilities and nothing else. This is why
   two episodes of *the same source* have different `capabilities_json` — the fact the schema was
   built to express.

3. **The signals disagree about time.** The pose layer is indexed on video frames; the IMU
   samples at ~195-198 Hz. Poses become columns of `frames.parquet`; the IMU becomes its own
   tables under `streams/` with their own `t` (invariant 17). Resampling one onto the other
   would fabricate samples. The gyroscope and the accelerometer are themselves **two** streams:
   upstream ships two clocks and they genuinely diverge (ADR 012).

4. **The pose is not measured.** It is a COLMAP reconstruction, so its channels are
   `origin: estimated`, frames the reconstruction failed on are **NaN — never 0** (a zero pose is
   a *place*), and a FAIL from a rule reading only those channels is downgraded to REVIEW by the
   engine (invariant 13).

**Two frame numberings, never mixed** (ADR 010). The annotation CSV's `start_frame`/`stop_frame`
are indices into a frame extraction that ran at 50 fps for 50 fps videos and a flat 60 fps for
everything else — reproduced exactly on all 67,217 train segments in M0, and *not* the video's
official rate. EPIC-Fields indexes its poses at the **official** fps. This adapter stores the
official-fps numbering, because that is the one the pose layer can be joined on, and preserves
the CSV's numbering verbatim in `raw_extra` as a different thing with a different name.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from rdp.application.ports import EpisodeRef, RawEpisode
from rdp.domain.action_spec import SignalLevel, SignalOrigin, SignalSpec
from rdp.domain.boundary import EndReason, EpisodeBoundary, SuccessAdjudicator, TerminationSource
from rdp.domain.camera import CameraEncoding, CameraMount, CameraSpec
from rdp.domain.capabilities import Capabilities
from rdp.domain.embodiment import Embodiment, EmbodimentRegistry
from rdp.domain.episode import CanonicalEpisode, EpisodeMeta, make_uid
from rdp.domain.errors import InvariantViolation
from rdp.domain.frames import FrameTable
from rdp.domain.provenance import ANNOTATION_SECONDS, Provenance
from rdp.domain.source import Source
from rdp.infrastructure.sources.staging import is_staged, mark_staged
from rdp.infrastructure.sources.upstream_fetch import UpstreamFetcher, UpstreamNotFound
from rdp.infrastructure.storage.atomic_fs import atomic_write_text

ADAPTER_VERSION = "epic@1.1.0"

ANNOTATIONS_LAYER = "annotations"
CAMERA_POSE_LAYER = "camera_pose"
IMU_LAYER = "imu"

VIDEO_INFO_PATH = "EPIC_100_video_info.csv"
SEGMENTS_PATH = "EPIC_100_train.csv"

REF_FILE = "ref.json"
POSE_FILE = "pose.json"
IMU_FILE = "imu.json"

GYRO_STREAM = "gyro"
ACCEL_STREAM = "accel"

_POSE_ORDER: tuple[str, ...] = (
    "cam_q.w",
    "cam_q.x",
    "cam_q.y",
    "cam_q.z",
    "cam_t.x",
    "cam_t.y",
    "cam_t.z",
)
"""EPIC-Fields stores each pose as `[qw, qx, qy, qz, tx, ty, tz]`. Measured in M0, not assumed
from the field name — the JSON calls the container `images` and the key `frame_….jpg`."""

_IMU_ORDER: dict[str, tuple[str, ...]] = {
    GYRO_STREAM: ("gyro.x", "gyro.y", "gyro.z"),
    ACCEL_STREAM: ("accel.x", "accel.y", "accel.z"),
}

_IMU_FILES: dict[str, str] = {GYRO_STREAM: "gyro", ACCEL_STREAM: "accl"}
"""Upstream spells the accelerometer `accl`. Our name for it is `accel`; the mapping is here
and nowhere else."""

_IMU_COLUMNS: dict[str, tuple[str, ...]] = {
    GYRO_STREAM: ("GyroX", "GyroY", "GyroZ"),
    ACCEL_STREAM: ("AcclX", "AcclY", "AcclZ"),
}
_MILLISECONDS = "Milliseconds"


class EpicKitchensAdapter:
    """Implements `SourcePort` for EPIC-KITCHENS-100, one action segment per episode."""

    def __init__(self, fetcher: UpstreamFetcher, embodiments: EmbodimentRegistry) -> None:
        self._fetcher = fetcher
        self._embodiments = embodiments
        # Upstream ships one file per *video* while an episode is one *segment*, so without a
        # cache a 20-episode run would parse the same 18 MB reconstruction twenty times.
        self._pose_cache: dict[str, Mapping[str, Any] | None] = {}
        self._imu_cache: dict[str, dict[str, dict[str, list[float]]] | None] = {}

    @property
    def kind(self) -> str:
        return "epic_kitchens"

    @property
    def adapter_version(self) -> str:
        return ADAPTER_VERSION

    # -- discovery ---------------------------------------------------------------------

    def list_episodes(self, source: Source) -> Iterator[EpisodeRef]:
        videos = self._video_info(source)
        wanted = [str(v) for v in source.options.get("videos", ())]
        if not wanted:
            raise InvariantViolation(
                f"{source.source_id}: list the video ids to ingest under `videos` in "
                "config/sources.yaml; EPIC-100 has 700 videos and no meaningful default order"
            )
        unknown = [video for video in wanted if video not in videos]
        if unknown:
            raise InvariantViolation(f"{source.source_id}: not in {VIDEO_INFO_PATH}: {unknown}")

        by_video: dict[str, list[dict[str, str]]] = {video: [] for video in wanted}
        for row in self._segments(source):
            if row["video_id"] in by_video:
                by_video[row["video_id"]].append(row)

        # Round-robin, not video-by-video: `--max-episodes 20` must still span a video that has
        # IMU and one that does not, or the capability heterogeneity never shows up in a run.
        for group in zip(*(by_video[video] for video in wanted), strict=False):
            for row in group:
                yield self._ref(source, row, videos[row["video_id"]])

    def _ref(
        self, source: Source, row: Mapping[str, str], video: Mapping[str, str]
    ) -> EpisodeRef:
        official_fps = float(video["fps"])
        start_s = _seconds(row["start_timestamp"])
        stop_s = _seconds(row["stop_timestamp"])
        first, last = _frame_range(start_s, stop_s, official_fps)
        return EpisodeRef(
            source_id=source.source_id,
            upstream_id=row["narration_id"],
            n_frames_hint=last - first + 1,
            extra={
                "video_id": row["video_id"],
                "participant_id": row["participant_id"],
                "official_fps": official_fps,
                "extraction_fps": _extraction_fps(source, official_fps),
                "start_s": start_s,
                "stop_s": stop_s,
                "first_frame": first,
                "last_frame": last,
                "segment": dict(row),
                "video_info": dict(video),
            },
        )

    # -- fetch -------------------------------------------------------------------------

    def fetch(self, ref: EpisodeRef, source: Source, dest: Path) -> RawEpisode:
        """Stage every enabled layer, and record which ones actually existed.

        Availability is *measured here and written down*, not re-probed in `normalize`: a resume
        must reach the same conclusion offline, and "EPIC-Fields was published between the two
        runs" must look like a content change, not like nondeterminism.
        """
        if is_staged(dest, self.adapter_version):
            return RawEpisode(ref=ref, path=dest, upstream_revision=source.revision)
        enabled = _enabled_layers(source)
        video_id = str(ref.extra["video_id"])
        first, last = int(ref.extra["first_frame"]), int(ref.extra["last_frame"])
        layers = {ANNOTATIONS_LAYER: True}

        pose = None
        if CAMERA_POSE_LAYER in enabled:
            pose = self._pose_for(source, video_id)
        layers[CAMERA_POSE_LAYER] = pose is not None
        if pose is not None:
            atomic_write_text(
                dest / POSE_FILE,
                json.dumps(_slice_pose(pose, first, last), sort_keys=True),
            )

        imu = None
        if IMU_LAYER in enabled:
            imu = self._imu_for(source, video_id, str(ref.extra["participant_id"]))
        layers[IMU_LAYER] = imu is not None
        if imu is not None:
            start_s, stop_s = float(ref.extra["start_s"]), float(ref.extra["stop_s"])
            window = {
                stream_id: _slice_imu(samples, start_s, stop_s)
                for stream_id, samples in imu.items()
            }
            atomic_write_text(dest / IMU_FILE, json.dumps(window, sort_keys=True))

        atomic_write_text(
            dest / REF_FILE,
            json.dumps(
                {
                    "source_id": ref.source_id,
                    "upstream_id": ref.upstream_id,
                    "extra": dict(ref.extra),
                    "layers": layers,
                    "revision": source.revision,
                },
                indent=2,
                sort_keys=True,
            ),
        )
        mark_staged(dest, self.adapter_version, layers=layers)
        return RawEpisode(ref=ref, path=dest, upstream_revision=source.revision)

    def _pose_for(self, source: Source, video_id: str) -> Mapping[str, Any] | None:
        if video_id not in self._pose_cache:
            layer = _layer_source(source, "camera_pose_uri")
            try:
                path = self._fetcher.local_path(layer, f"{video_id}.json")
            except UpstreamNotFound:
                self._pose_cache[video_id] = None
            else:
                document = json.loads(path.read_text())
                # `points` is the sparse point cloud of the whole video — tens of MB that say
                # nothing about any one segment. Only `images` (the per-frame poses) is kept.
                self._pose_cache[video_id] = document.get("images", {})
        return self._pose_cache[video_id]

    def _imu_for(
        self, source: Source, video_id: str, participant_id: str
    ) -> dict[str, dict[str, list[float]]] | None:
        if video_id not in self._imu_cache:
            layer = _layer_source(source, "imu_uri")
            try:
                samples = {
                    stream_id: _read_imu_csv(
                        self._fetcher.local_path(
                            layer,
                            f"{participant_id}/meta_data/{video_id}-{_IMU_FILES[stream_id]}.csv",
                        ),
                        _IMU_COLUMNS[stream_id],
                    )
                    for stream_id in _IMU_ORDER
                }
            except UpstreamNotFound:
                # The EPIC-55 era videos have no GoPro metadata at all. Absent, not empty.
                self._imu_cache[video_id] = None
            else:
                self._imu_cache[video_id] = samples
        return self._imu_cache[video_id]

    # -- normalize ---------------------------------------------------------------------

    def normalize(self, raw: RawEpisode, source: Source) -> CanonicalEpisode:
        staged = json.loads((raw.path / REF_FILE).read_text())
        extra = staged["extra"]
        layers: Mapping[str, bool] = staged["layers"]
        embodiment = self._embodiments.get(source.embodiment)

        official_fps = float(extra["official_fps"])
        first, last = int(extra["first_frame"]), int(extra["last_frame"])
        frame_index = np.arange(first, last + 1, dtype=np.int64)
        # Both clocks are measured from the presentation time of the episode's first frame, so
        # the IMU stream and the frame table share an origin without either being resampled.
        origin_s = first / official_fps
        columns: dict[str, NDArray[Any]] = {
            "t": (frame_index - first).astype(np.float64) / official_fps,
            "raw.frame_index": frame_index,
        }

        has_pose = bool(layers.get(CAMERA_POSE_LAYER))
        state_spec = embodiment.state_spec()
        if has_pose:
            poses = json.loads((raw.path / POSE_FILE).read_text())
            columns.update(_pose_columns(state_spec, poses, frame_index))
        else:
            # Not zero-filled and not an empty column: the signal does not exist for this
            # episode, and invariant 3 makes `has_state=False` the only consistent statement.
            state_spec = SignalSpec(is_command=False, level=SignalLevel.ABSENT)

        frames = FrameTable(columns=columns, raw_frame_columns=("raw.frame_index",))

        streams: dict[str, FrameTable] = {}
        stream_specs: dict[str, SignalSpec] = {}
        if layers.get(IMU_LAYER):
            window = json.loads((raw.path / IMU_FILE).read_text())
            for stream_id in _IMU_ORDER:
                stream_specs[stream_id] = embodiment.stream_spec(stream_id)
                streams[stream_id] = _imu_table(
                    stream_id, stream_specs[stream_id], window[stream_id], origin_s
                )

        meta = self._meta(staged, source, embodiment, frames, state_spec, stream_specs, layers)
        return CanonicalEpisode(meta=meta, frames=frames, streams=streams)

    def _meta(
        self,
        staged: Mapping[str, Any],
        source: Source,
        embodiment: Embodiment,
        frames: FrameTable,
        state_spec: SignalSpec,
        stream_specs: Mapping[str, SignalSpec],
        layers: Mapping[str, bool],
    ) -> EpisodeMeta:
        extra = staged["extra"]
        segment: Mapping[str, str] = extra["segment"]
        video: Mapping[str, str] = extra["video_info"]
        official_fps = float(extra["official_fps"])
        upstream_id = str(staged["upstream_id"])
        has_pose = bool(layers.get(CAMERA_POSE_LAYER))
        has_imu = bool(layers.get(IMU_LAYER))

        capabilities = Capabilities(
            # The label *is* the action, so the capability is present while the level is not
            # per-frame. Anything else would make invariant 3 unsatisfiable for this source.
            has_action=True,
            has_state=has_pose,
            has_camera_pose=has_pose,
            has_imu=has_imu,
            has_language=True,
            # No pixels are fetched: the videos are ~700 GB and nothing here needs them. The
            # camera is declared so the topology is legible, with `is_present=False`.
            has_rgb=False,
            has_video=False,
            has_gripper=False,
            has_reward=False,
            has_termination_signal=False,
            is_real_robot=embodiment.is_real_robot,
            is_teleop=embodiment.is_teleop,
        )

        signal_origin = {"task": SignalOrigin.ANNOTATED}
        if has_pose:
            signal_origin["state"] = SignalOrigin.ESTIMATED
        if has_imu:
            for stream_id in _IMU_ORDER:
                signal_origin[stream_id] = SignalOrigin.MEASURED

        return EpisodeMeta(
            uid=make_uid(source.source_id, upstream_id),
            source_id=source.source_id,
            upstream_id=upstream_id,
            embodiment=embodiment.embodiment_id,
            task=f"{segment['verb']} {segment['noun']}",
            n_frames=frames.n_frames,
            action_spec=embodiment.action_spec(),
            state_spec=state_spec,
            stream_specs=dict(stream_specs),
            capabilities=capabilities,
            cameras=(_camera(video),),
            provenance=Provenance(
                is_original=True,
                # The authority is the annotator's seconds; the frame axis is derived from them.
                timestamp_source=ANNOTATION_SECONDS,
                # Invariant 15: the rate is part of the statement. A bare "derived" is rejected
                # by the domain, because it cannot be undone or checked.
                frame_index_source=f"derived_from_seconds@{official_fps:g}",
                signal_origin=signal_origin,
                transforms=_transforms(layers),
                mirrors=tuple(source.options.get("mirrors", ())),
                upstream_revision=str(staged["revision"]),
                adapter_version=self.adapter_version,
            ),
            boundary=EpisodeBoundary(
                termination_source=TerminationSource.ANNOTATOR,
                end_reason=EndReason.ANNOTATION_BOUND,
                # The segment ends where the annotator said the action ended. That is neither a
                # time limit nor a cut: nothing was lost, so `is_truncated` is False.
                is_truncated=False,
                # Nobody ever judged whether "open door" succeeded, and no simulator can.
                success=None,
                success_adjudicator=SuccessAdjudicator.NONE,
            ),
            fps_nominal=official_fps,
            fps_effective=official_fps,
            duration_s=float(frames.t[-1] - frames.t[0]) if frames.n_frames > 1 else 0.0,
            raw_extra={
                "epic": {
                    "narration": segment.get("narration"),
                    "verb": segment.get("verb"),
                    "verb_class": segment.get("verb_class"),
                    "noun": segment.get("noun"),
                    "noun_class": segment.get("noun_class"),
                    "all_nouns": segment.get("all_nouns"),
                    "narration_timestamp": segment.get("narration_timestamp"),
                    "start_timestamp": segment.get("start_timestamp"),
                    "stop_timestamp": segment.get("stop_timestamp"),
                    "video_id": extra["video_id"],
                    "participant_id": extra["participant_id"],
                    "official_fps": official_fps,
                    "layers": dict(layers),
                    # A DIFFERENT numbering of the same seconds, kept under its own name so it
                    # can never be confused with `raw.frame_index` (ADR 010).
                    "extraction_numbering": {
                        "start_frame": segment.get("start_frame"),
                        "stop_frame": segment.get("stop_frame"),
                        "fps": extra["extraction_fps"],
                        "note": (
                            "annotation-CSV frame indices, at the extraction fps, not the "
                            "official fps; not comparable with raw.frame_index"
                        ),
                    },
                }
            },
            raw_frame_columns=frames.raw_frame_columns,
        )

    # -- upstream metadata -------------------------------------------------------------

    def _video_info(self, source: Source) -> dict[str, dict[str, str]]:
        rows = _read_csv(self._fetcher.local_path(source, VIDEO_INFO_PATH))
        return {row["video_id"]: row for row in rows}

    def _segments(self, source: Source) -> list[dict[str, str]]:
        return _read_csv(self._fetcher.local_path(source, SEGMENTS_PATH))


# -- upstream layout helpers -------------------------------------------------------------


def _layer_source(source: Source, option: str) -> Source:
    """One `Source` per layer, so the shared fetcher can serve three different servers."""
    uri = source.options.get(option)
    if not uri:
        raise InvariantViolation(f"{source.source_id}: `{option}` is not configured")
    return source.model_copy(update={"uri": str(uri)})


def _enabled_layers(source: Source) -> frozenset[str]:
    declared = source.options.get("layers")
    if not declared:
        return frozenset({ANNOTATIONS_LAYER})
    return frozenset(str(layer) for layer in declared)


def _extraction_fps(source: Source, official_fps: float) -> float:
    """The rate the released JPEGs were extracted at — *not* the video's rate (ADR 004)."""
    rule = source.options.get("frame_extraction_fps", {})
    if official_fps == 50.0:
        return float(rule.get("when_official_fps_is_50", 50))
    return float(rule.get("otherwise", 60))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _seconds(timestamp: str) -> float:
    """`HH:MM:SS.ff` -> seconds. The authoritative time; frame indices are derived from it."""
    hours, minutes, seconds = timestamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _frame_range(start_s: float, stop_s: float, fps: float) -> tuple[int, int]:
    """Inclusive, 0-based, on the OFFICIAL fps — the clock the pose layer is indexed on."""
    first = int(start_s * fps)
    last = max(first, int(stop_s * fps))
    return first, last


# -- camera pose layer ---------------------------------------------------------------------


def _pose_key(frame_index: int) -> str:
    """EPIC-Fields keys are the extracted JPEG filenames, and those are **1-based**."""
    return f"frame_{frame_index + 1:010d}.jpg"


def _slice_pose(images: Mapping[str, Any], first: int, last: int) -> dict[str, Any]:
    keys = (_pose_key(index) for index in range(first, last + 1))
    return {key: images[key] for key in keys if key in images}


def _pose_columns(
    spec: SignalSpec, poses: Mapping[str, Sequence[float]], frame_index: NDArray[Any]
) -> dict[str, NDArray[Any]]:
    # A pose is 7 numbers in a fixed order; mapping them by name is the only way a reordering
    # of embodiments.yaml cannot silently swap rotation for translation.
    declared = {channel.name for channel in spec.channels}
    if declared != set(_POSE_ORDER):
        raise InvariantViolation(
            f"human_ego state channels {sorted(declared)} != the EPIC-Fields pose layout "
            f"{sorted(_POSE_ORDER)}; config/embodiments.yaml and this adapter must agree"
        )
    values = np.full((frame_index.shape[0], len(_POSE_ORDER)), np.nan, dtype=np.float64)
    for row, index in enumerate(frame_index):
        pose = poses.get(_pose_key(int(index)))
        if pose is None:
            continue  # unregistered: stays NaN, and is stored as a parquet NULL
        if len(pose) != len(_POSE_ORDER):
            raise InvariantViolation(f"pose for frame {index} has {len(pose)} values, expected 7")
        values[row] = pose
    return {
        f"state.{channel.name}": values[:, _POSE_ORDER.index(channel.name)]
        for channel in spec.channels
    }


# -- IMU layer -----------------------------------------------------------------------------


def _read_imu_csv(path: Path, columns: Sequence[str]) -> dict[str, list[float]]:
    rows = _read_csv(path)
    return {
        _MILLISECONDS: [float(row[_MILLISECONDS]) for row in rows],
        **{name: [float(row[name]) for row in rows] for name in columns},
    }


def _slice_imu(
    samples: Mapping[str, list[float]], start_s: float, stop_s: float
) -> dict[str, list[float]]:
    ms = np.asarray(samples[_MILLISECONDS], dtype=np.float64)
    window = (ms >= start_s * 1000.0) & (ms <= stop_s * 1000.0)
    return {
        name: np.asarray(values, dtype=np.float64)[window].tolist()
        for name, values in samples.items()
    }


def _imu_table(
    stream_id: str, spec: SignalSpec, window: Mapping[str, list[float]], origin_s: float
) -> FrameTable:
    order = _IMU_ORDER[stream_id]
    declared = tuple(channel.name for channel in spec.channels)
    if declared != order:
        raise InvariantViolation(
            f"human_ego {stream_id} channels {list(declared)} != {list(order)}; "
            "config/embodiments.yaml and this adapter must agree"
        )
    columns: dict[str, NDArray[Any]] = {
        "t": np.asarray(window[_MILLISECONDS], dtype=np.float64) / 1000.0 - origin_s
    }
    for name, upstream in zip(order, _IMU_COLUMNS[stream_id], strict=True):
        columns[f"state.{name}"] = np.asarray(window[upstream], dtype=np.float64)
    return FrameTable(columns=columns)


# -- meta helpers ---------------------------------------------------------------------------


def _camera(video: Mapping[str, str]) -> CameraSpec:
    height, width = _resolution(video.get("resolution", ""))
    return CameraSpec(
        name="head",
        mount=CameraMount.HEAD,
        resolution=(height, width),
        channels=3,
        # Declared upstream, deliberately not downloaded. `is_present=False` keeps every
        # pixel-reading rule honestly SKIPPED instead of failing on a file we never fetched.
        encoding=CameraEncoding.ABSENT,
        is_present=False,
    )


def _resolution(text: str) -> tuple[int, int]:
    width, _, height = text.partition("x")
    try:
        return int(height), int(width)
    except ValueError:
        return 0, 0


def _transforms(layers: Mapping[str, bool]) -> tuple[dict[str, Any], ...]:
    """A layer that was probed and absent is a recorded fact, not a silent omission."""
    missing = sorted(name for name, present in layers.items() if not present)
    if not missing:
        return ()
    return (
        {
            "kind": "layer_unavailable",
            "layers": missing,
            "effect": "the capabilities those layers would set are False for this episode",
        },
    )
