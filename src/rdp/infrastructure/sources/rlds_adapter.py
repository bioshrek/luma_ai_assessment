"""RLDS / Open X-Embodiment adapter — source C (`berkeley_autolab_ur5`).

Read directly from TFRecord shards, with no TensorFlow anywhere in the dependency set
([ADR 001](../../../../docs/adr/001-rlds-reader-no-tensorflow.md)). Three things make this
source structurally different from the LeRobot ones, and all three are decided in
[ADR 009](../../../../docs/adr/009-m3-rlds-identity-clock-and-padding.md):

1. **No stable upstream id.** An episode's only handle is its position in a shard, so identity
   is the episode's index within the *split*, which survives a re-shard; the shard file and the
   index inside it are locators, not identity.
2. **No clock at all.** `t` is synthesized from a configured control rate, so every timestamp
   rule degrades to `SKIPPED` rather than passing by construction.
3. **A trailing boundary step** whose action is a placeholder. It is trimmed, and everything it
   carried is hoisted to episode level rather than discarded.

The 8-D action layout — 3 translation deltas, 3 rotation deltas, 1 gripper *change* command and
1 non-physical control flag — is our flattening of an upstream dict, so `ACTION_KEYS` below is a
public contract, not an implementation detail (ADR 003).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from rdp.application.ports import EpisodeRef, RawEpisode
from rdp.domain.action_spec import SignalOrigin
from rdp.domain.boundary import EndReason, EpisodeBoundary, SuccessAdjudicator, TerminationSource
from rdp.domain.camera import CameraEncoding, CameraMount, CameraSpec
from rdp.domain.capabilities import Capabilities
from rdp.domain.embodiment import Embodiment, EmbodimentRegistry
from rdp.domain.episode import CanonicalEpisode, EpisodeMeta, make_uid
from rdp.domain.errors import InvariantViolation
from rdp.domain.frames import FrameTable
from rdp.domain.provenance import Provenance, synthesized_at
from rdp.domain.source import Source
from rdp.infrastructure.sources.staging import is_staged, mark_staged
from rdp.infrastructure.sources.tfrecord import Feature, iter_records, parse_example
from rdp.infrastructure.sources.upstream_fetch import UpstreamFetcher
from rdp.infrastructure.storage.atomic_fs import atomic_write_bytes, atomic_write_text

ADAPTER_VERSION = "rlds@1.1.0"

DATASET_INFO_PATH = "dataset_info.json"
FEATURES_PATH = "features.json"
SUPPORTED_FILE_FORMAT = "tfrecord"

RECORD_FILE = "record.pb"
REF_FILE = "ref.json"

TERMINAL_KEY = "steps/is_terminal"
TERMINAL_COLUMN = "raw.is_terminal"
"""RLDS's end-of-episode marker, upstream and as stored. Named here so no rule guesses it."""

ACTION_KEYS: tuple[tuple[str, int], ...] = (
    ("steps/action/world_vector", 3),
    ("steps/action/rotation_delta", 3),
    ("steps/action/gripper_closedness_action", 1),
    ("steps/action/terminate_episode", 1),
)
"""Upstream `action` is a dict with no order. This tuple *is* the flattening contract, and it
must stay in step with `ur5_single_arm.action` in `config/embodiments.yaml`."""

STATE_KEY = "steps/observation/robot_state"
INSTRUCTION_KEY = "steps/observation/natural_language_instruction"
POSE_KEYS = ("steps/action/world_vector", "steps/action/rotation_delta")

_STEP_PREFIX = "steps/"
_OBSERVATION_PREFIX = "steps/observation/"


class RLDSAdapter:
    """Implements `SourcePort` for any RLDS dataset laid out as TFDS TFRecord shards."""

    def __init__(
        self,
        fetcher: UpstreamFetcher,
        embodiments: EmbodimentRegistry,
        shard_layout_revision: str | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._embodiments = embodiments
        self._shard_layout_revision = shard_layout_revision

    @property
    def kind(self) -> str:
        return "rlds"

    @property
    def adapter_version(self) -> str:
        """The shard layout is part of how we read upstream, so it belongs in the staleness key.

        Episode identity deliberately does *not* depend on it, so a re-shard can never make the
        existing corpus look new; declaring the new layout makes it look **stale** instead, and
        the re-normalization re-verifies every `content_hash` (ADR 009, decision 2).
        """
        if not self._shard_layout_revision:
            return ADAPTER_VERSION
        return f"{ADAPTER_VERSION}+layout={self._shard_layout_revision}"

    # -- discovery ---------------------------------------------------------------------

    def list_episodes(self, source: Source) -> Iterator[EpisodeRef]:
        info = self._dataset_info(source)
        if info.get("fileFormat") != SUPPORTED_FILE_FORMAT:
            # ADR 001: the reader owns the container format, so it must assert it, not assume it.
            raise InvariantViolation(
                f"{source.source_id}: fileFormat is {info.get('fileFormat')!r}, "
                f"this reader only handles {SUPPORTED_FILE_FORMAT!r}"
            )
        split_name = str(source.options.get("split", "train"))
        split = _split(info, split_name)
        shard_lengths = [int(n) for n in split["shardLengths"]]
        measured_layout = _layout_revision(split_name, len(shard_lengths), str(info["version"]))
        dataset = str(info["name"])

        index = 0
        for shard_index, length in enumerate(shard_lengths):
            shard_name = _shard_name(dataset, split_name, shard_index, len(shard_lengths))
            for position in range(length):
                yield EpisodeRef(
                    source_id=source.source_id,
                    upstream_id=f"{split_name}#{index:06d}",
                    extra={
                        "split": split_name,
                        "episode_index": index,
                        "shard_name": shard_name,
                        "shard_index": shard_index,
                        "index_in_shard": position,
                        "measured_shard_layout": measured_layout,
                        "declared_shard_layout": source.shard_layout_revision,
                    },
                )
                index += 1

    # -- fetch -------------------------------------------------------------------------

    def fetch(self, ref: EpisodeRef, source: Source, dest: Path) -> RawEpisode:
        if is_staged(dest, self.adapter_version):
            return RawEpisode(ref=ref, path=dest, upstream_revision=source.revision)

        record = self._read_record(
            source, str(ref.extra["shard_name"]), int(ref.extra["index_in_shard"])
        )
        atomic_write_bytes(dest / RECORD_FILE, record)
        atomic_write_text(
            dest / REF_FILE,
            json.dumps(
                {
                    "source_id": ref.source_id,
                    "upstream_id": ref.upstream_id,
                    "extra": dict(ref.extra),
                    # Carried along so `normalize` needs no network on a resume.
                    "features": self._features(source),
                    "revision": source.revision,
                },
                indent=2,
                sort_keys=True,
            ),
        )
        mark_staged(dest, self.adapter_version, record_bytes=len(record))
        return RawEpisode(ref=ref, path=dest, upstream_revision=source.revision)

    def _read_record(self, source: Source, shard_name: str, index_in_shard: int) -> bytes:
        """Stream the shard and stop at the wanted record — never buffer the whole 180 MB."""
        with self._fetcher.open_stream(source, shard_name) as stream:
            for position, record in enumerate(iter_records(stream)):
                if position == index_in_shard:
                    return record
        raise InvariantViolation(
            f"{shard_name}: no record at index {index_in_shard}; the shard layout changed"
        )

    # -- normalize ---------------------------------------------------------------------

    def normalize(self, raw: RawEpisode, source: Source) -> CanonicalEpisode:
        staged = json.loads((raw.path / REF_FILE).read_text())
        example = parse_example((raw.path / RECORD_FILE).read_bytes())
        embodiment = self._embodiments.get(source.embodiment)
        dropped = tuple(str(name) for name in source.options.get("drop_channels", ()))

        n_upstream = _step_count(example)
        keep = _keep_count(example, n_upstream)

        action = _flatten(example, ACTION_KEYS, n_upstream)[:keep]
        state = _matrix(example, STATE_KEY, n_upstream)[:keep]
        embodiment.assert_width(action_dim=action.shape[1], state_dim=state.shape[1])

        control_hz = _control_hz(source)
        columns: dict[str, NDArray[Any]] = {
            "t": np.arange(keep, dtype=np.float64) / control_hz
        }
        columns.update(_named(embodiment.action_channels, "action", action))
        columns.update(_named(embodiment.state_channels, "state", state))

        raw_columns: list[str] = []
        for name, values in _scalar_step_features(example, n_upstream, dropped):
            columns[f"raw.{name}"] = values[:keep]
            raw_columns.append(f"raw.{name}")

        frames = FrameTable(columns=columns, raw_frame_columns=tuple(raw_columns))
        meta = self._meta(
            staged, example, frames, source, embodiment, n_upstream, keep, control_hz, dropped
        )
        return CanonicalEpisode(meta=meta, frames=frames)

    def _meta(
        self,
        staged: Mapping[str, Any],
        example: Mapping[str, Feature],
        frames: FrameTable,
        source: Source,
        embodiment: Embodiment,
        n_upstream: int,
        keep: int,
        control_hz: float,
        dropped: tuple[str, ...],
    ) -> EpisodeMeta:
        extra = dict(staged["extra"])
        features = staged["features"]
        cameras = _cameras(features, example)
        instruction = _instruction(example)
        trimmed = _trimmed_summary(example, keep, n_upstream)

        capabilities = Capabilities(
            has_action=True,
            has_state=True,
            has_gripper=True,
            # Pixels exist, but as frames inlined in the record — there is no decodable video
            # file, so `has_video` stays False and every video rule degrades to SKIPPED.
            has_rgb=any(c.channels == 3 for c in cameras),
            has_video=False,
            has_language=instruction is not None,
            has_reward="steps/reward" in example,
            has_depth=any(c.channels == 1 for c in cameras),
            # Measured on the rows we kept, not on the ones upstream shipped. ADR 009's padding
            # trim removes precisely the steps that carry `is_terminal`, so declaring the
            # capability from the record's schema would hand `TERMINATION_CONSISTENCY` a column
            # that is all-false by our own doing and earn a REVIEW on every episode. A fact we
            # deleted is a fact we no longer have (ADR 015).
            has_termination_signal=_has_end_marker(example, keep),
            is_real_robot=embodiment.is_real_robot,
            is_teleop=embodiment.is_teleop,
        )

        return EpisodeMeta(
            uid=make_uid(source.source_id, str(staged["upstream_id"])),
            source_id=source.source_id,
            upstream_id=str(staged["upstream_id"]),
            embodiment=embodiment.embodiment_id,
            task=instruction,
            n_frames=frames.n_frames,
            action_spec=embodiment.action_spec(),
            state_spec=embodiment.state_spec(),
            capabilities=capabilities,
            cameras=cameras,
            provenance=Provenance(
                is_original=True,
                # There is no timestamp field anywhere in RLDS; the clock is ours (ADR 009).
                timestamp_source=synthesized_at(control_hz),
                frame_index_source="upstream",
                signal_origin={
                    "action": SignalOrigin.MEASURED,
                    "state": SignalOrigin.MEASURED,
                },
                transforms=_transforms(dropped, trimmed, cameras),
                upstream_revision=str(staged["revision"]),
                adapter_version=self.adapter_version,
            ),
            boundary=_boundary(example),
            fps_nominal=control_hz,
            fps_effective=control_hz,
            duration_s=float(frames.t[-1] - frames.t[0]) if frames.n_frames > 1 else 0.0,
            termination_column=(
                TERMINAL_COLUMN
                if capabilities.has_termination_signal
                and TERMINAL_COLUMN in frames.raw_frame_columns
                else None
            ),
            raw_extra={
                "rlds": {
                    "split": extra.get("split"),
                    "episode_index": extra.get("episode_index"),
                    "shard_name": extra.get("shard_name"),
                    "index_in_shard": extra.get("index_in_shard"),
                    "measured_shard_layout": extra.get("measured_shard_layout"),
                    "declared_shard_layout": extra.get("declared_shard_layout"),
                    "n_steps_upstream": n_upstream,
                    "language_instruction": instruction,
                    **trimmed,
                }
            },
            raw_frame_columns=frames.raw_frame_columns,
        )

    # -- upstream metadata ---------------------------------------------------------------

    def _dataset_info(self, source: Source) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(
            self._fetcher.local_path(source, DATASET_INFO_PATH).read_text()
        )
        return payload

    def _features(self, source: Source) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(
            self._fetcher.local_path(source, FEATURES_PATH).read_text()
        )
        return payload


# -- upstream layout helpers -------------------------------------------------------------


def _split(info: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    for split in info.get("splits", []):
        if split.get("name") == name:
            found: Mapping[str, Any] = split
            return found
    available = [str(s.get("name")) for s in info.get("splits", [])]
    raise InvariantViolation(f"split {name!r} not in dataset_info.json; have {available}")


def _layout_revision(split: str, n_shards: int, version: str) -> str:
    return f"{split}:{n_shards}-shards@{version}"


def _shard_name(dataset: str, split: str, index: int, total: int) -> str:
    return f"{dataset}-{split}.tfrecord-{index:05d}-of-{total:05d}"


def _control_hz(source: Source) -> float:
    """Required, never defaulted: inventing a control rate would invent the whole time axis."""
    value = source.options.get("control_hz")
    if value is None:
        raise InvariantViolation(
            f"{source.source_id}: RLDS carries no timestamps, so `control_hz` must be declared "
            "in config/sources.yaml with the citation that justifies it"
        )
    return float(value)


# -- step decoding -------------------------------------------------------------------------


def _step_count(example: Mapping[str, Feature]) -> int:
    """Steps are flattened per feature, so the count comes from a known width-1 feature."""
    for key in ("steps/is_last", "steps/is_first", "steps/is_terminal", "steps/reward"):
        if key in example:
            return len(example[key])
    raise InvariantViolation("cannot determine the step count: no per-step boundary flag present")


def _has_end_marker(example: Mapping[str, Feature], keep: int) -> bool:
    """Did an end-of-episode marker survive the padding trim? See `_keep_count`."""
    terminal = example.get(TERMINAL_KEY)
    if terminal is None:
        return False
    return any(bool(value) for value in terminal.values[:keep])


def _keep_count(example: Mapping[str, Feature], n_upstream: int) -> int:
    """Trim the trailing RLDS boundary steps: flagged `is_last` with an all-zero pose action.

    Measured on `berkeley_autolab_ur5`: `is_last` is set on the final **two** steps, both of
    which carry a zero `world_vector` and `rotation_delta`. Keeping them would make every
    episode end with a fabricated "the robot stopped" motion. Genuine mid-episode zero actions
    are untouched because they are not flagged (ADR 009, decision 3).
    """
    is_last = example.get("steps/is_last")
    if is_last is None:
        return n_upstream
    poses = [
        np.asarray(example[key].values, dtype=np.float64).reshape(n_upstream, -1)
        for key in POSE_KEYS
        if key in example
    ]
    keep = n_upstream
    while keep > 0:
        index = keep - 1
        if not is_last.values[index]:
            break
        if any(pose[index].any() for pose in poses):
            break
        keep -= 1
    return keep


def _matrix(example: Mapping[str, Feature], key: str, n_steps: int) -> NDArray[Any]:
    feature = example.get(key)
    if feature is None:
        raise InvariantViolation(f"missing per-step feature {key!r}")
    values = np.asarray(feature.values, dtype=np.float64)
    if values.size % n_steps:
        raise InvariantViolation(
            f"{key!r}: {values.size} values do not divide {n_steps} steps evenly"
        )
    return values.reshape(n_steps, values.size // n_steps)


def _flatten(
    example: Mapping[str, Feature], keys: Sequence[tuple[str, int]], n_steps: int
) -> NDArray[Any]:
    parts = []
    for key, width in keys:
        block = _matrix(example, key, n_steps)
        if block.shape[1] != width:
            raise InvariantViolation(
                f"{key!r}: upstream width {block.shape[1]} != declared {width}"
            )
        parts.append(block)
    return np.concatenate(parts, axis=1)


def _named(
    channels: Sequence[Any], prefix: str, values: NDArray[Any]
) -> dict[str, NDArray[Any]]:
    return {f"{prefix}.{channel.name}": values[:, i] for i, channel in enumerate(channels)}


def _scalar_step_features(
    example: Mapping[str, Feature], n_steps: int, dropped: Sequence[str]
) -> Iterator[tuple[str, NDArray[Any]]]:
    """Everything per-step we did not model becomes a `raw.` column (design §2.4).

    Byte features (the inline camera frames, the instruction string) are excluded: the frame
    table is numeric, and both are represented elsewhere — as `CameraSpec` and as `task`.
    """
    consumed = {key for key, _ in ACTION_KEYS} | {STATE_KEY, INSTRUCTION_KEY, *dropped}
    for key in sorted(example):
        if not key.startswith(_STEP_PREFIX) or key in consumed:
            continue
        feature = example[key]
        if feature.kind == "bytes" or len(feature) != n_steps:
            continue
        name = key[len(_OBSERVATION_PREFIX) :] if key.startswith(_OBSERVATION_PREFIX) else key[
            len(_STEP_PREFIX) :
        ]
        yield name, np.asarray(feature.values, dtype=np.float64)


def _instruction(example: Mapping[str, Feature]) -> str | None:
    feature = example.get(INSTRUCTION_KEY)
    if feature is None or not feature.values:
        return None
    distinct = {bytes(value) for value in feature.values}
    if len(distinct) != 1:
        # A per-step instruction would be an episode-level `task` only by accident.
        return None
    return next(iter(distinct)).decode("utf-8", errors="replace")


def _trimmed_summary(
    example: Mapping[str, Feature], keep: int, n_upstream: int
) -> dict[str, Any]:
    """Nothing the trimmed steps carried is lost — it is hoisted to episode level."""
    rewards = example.get("steps/reward")
    terminal = example.get("steps/is_terminal")
    return {
        "n_trailing_boundary_steps_trimmed": n_upstream - keep,
        "trimmed_step_rewards": (
            [float(v) for v in rewards.values[keep:]] if rewards is not None else []
        ),
        "trimmed_step_is_terminal": (
            [bool(v) for v in terminal.values[keep:]] if terminal is not None else []
        ),
        "terminal_reward": (
            float(max(rewards.values)) if rewards is not None and rewards.values else None
        ),
    }


def _transforms(
    dropped: Sequence[str], trimmed: Mapping[str, Any], cameras: Sequence[CameraSpec]
) -> tuple[dict[str, Any], ...]:
    transforms: list[dict[str, Any]] = []
    if dropped:
        transforms.append(
            {
                "op": "drop_channels",
                "columns": list(dropped),
                "reason": "recomputable derivative bound to an encoder version",
            }
        )
    if trimmed["n_trailing_boundary_steps_trimmed"]:
        transforms.append(
            {
                "op": "trim_trailing_steps",
                "n": trimmed["n_trailing_boundary_steps_trimmed"],
                "reason": "RLDS boundary steps: is_last with an all-zero pose action",
            }
        )
    if cameras:
        transforms.append(
            {
                "op": "drop_inline_frames",
                "columns": [camera.name for camera in cameras],
                "reason": "pixels stay in raw/; the normalized store holds numeric channels",
            }
        )
    return tuple(transforms)


# -- episode-level metadata ------------------------------------------------------------------


def _cameras(
    features: Mapping[str, Any], example: Mapping[str, Feature]
) -> tuple[CameraSpec, ...]:
    cameras = []
    for name, node in sorted(_observation_features(features).items()):
        image = node.get("image")
        if image is None:
            continue
        shape = [int(d) for d in image.get("shape", {}).get("dimensions", [])]
        payload = example.get(f"{_OBSERVATION_PREFIX}{name}")
        cameras.append(
            CameraSpec(
                name=name,
                # Only an explicit name hint is trusted; upstream never states the topology.
                mount=CameraMount.WRIST if "hand" in name or "wrist" in name else (
                    CameraMount.STATIC
                ),
                resolution=(shape[0], shape[1]) if len(shape) >= 2 else (0, 0),
                channels=shape[2] if len(shape) > 2 else 0,
                encoding=CameraEncoding.INLINE_FRAMES,
                # Measured, not declared: a fixture with the pixels stripped must say False.
                is_present=bool(payload and any(payload.values)),
            )
        )
    return tuple(cameras)


def _observation_features(features: Mapping[str, Any]) -> dict[str, Any]:
    try:
        steps = features["featuresDict"]["features"]["steps"]["sequence"]["feature"]
        node = steps["featuresDict"]["features"]["observation"]
        result: dict[str, Any] = node["featuresDict"]["features"]
        return result
    except KeyError:
        return {}


def _boundary(example: Mapping[str, Feature]) -> EpisodeBoundary:
    is_last = example.get("steps/is_last")
    is_terminal = example.get("steps/is_terminal")
    truncated: bool | None = None
    if is_last is not None and is_terminal is not None:
        # `is_last & ~is_terminal` is the only place truncation is recoverable in RLDS.
        truncated = bool(any(is_last.values)) and not bool(any(is_terminal.values))
    return EpisodeBoundary(
        # The policy raises `terminate_episode`; the environment has no say.
        termination_source=TerminationSource.POLICY_FLAG,
        end_reason=EndReason.TRUNCATED if truncated else EndReason.UNKNOWN,
        is_truncated=truncated,
        # An adjudicator exists — the policy — but the dataset publishes no verdict. None here
        # means "unknown", which is the opposite of source D's "no adjudicator exists".
        success=None,
        success_adjudicator=SuccessAdjudicator.POLICY,
    )
