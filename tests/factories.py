"""Builders for valid domain objects, so each test states only what it is about."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

from rdp.domain.action_spec import (
    Channel,
    ChannelRole,
    ChannelSpace,
    GripperInverse,
    GripperSpec,
    ReferenceFrame,
    RotationRepr,
    RotationSpec,
    SignalLevel,
    SignalOrigin,
    SignalSpec,
    Unit,
)
from rdp.domain.boundary import EndReason, EpisodeBoundary, SuccessAdjudicator, TerminationSource
from rdp.domain.camera import CameraEncoding, CameraMount, CameraSpec
from rdp.domain.capabilities import Capabilities
from rdp.domain.episode import EpisodeMeta, make_uid
from rdp.domain.frames import FrameTable
from rdp.domain.provenance import Provenance
from rdp.domain.segment import EpisodeSegment


def camera(
    name: str = "top",
    encoding: CameraEncoding = CameraEncoding.MP4_SIDECAR,
    is_present: bool = True,
    n_frames: int | None = None,
) -> CameraSpec:
    return CameraSpec(
        name=name,
        mount=CameraMount.STATIC,
        resolution=(96, 96),
        channels=3,
        encoding=encoding,
        is_present=is_present,
        n_frames=n_frames,
    )


def gripper_channel(name: str = "gripper", is_delta: bool = False) -> Channel:
    """A gripper the way B declares it (absolute opening) or C does (a change command)."""
    return Channel(
        name=name,
        role=ChannelRole.GRIPPER,
        space=ChannelSpace.GRIPPER,
        origin=SignalOrigin.MEASURED,
        is_delta=is_delta,
        is_physical=True,
        unit=Unit.NORMALIZED,
        metric_convertible=False,
        group="gripper",
        gripper=GripperSpec(
            convention="normalized_unverified_direction",
            original_convention="normalized_unverified_direction",
            inverse=GripperInverse(scale=1.0, offset=0.0),
        ),
    )


def xy_channels() -> tuple[Channel, ...]:
    return tuple(
        Channel(
            name=f"ee.{axis}",
            role=ChannelRole.END_EFFECTOR,
            space=ChannelSpace.CARTESIAN_2D,
            origin=SignalOrigin.MEASURED,
            is_delta=False,
            is_physical=True,
            unit=Unit.PX,
            frame=ReferenceFrame.WORLD,
            group="ee_xy",
        )
        for axis in ("x", "y")
    )


def pose_channels() -> tuple[Channel, ...]:
    """A camera pose the way source D declares it: estimated, unitless, not metric."""
    translation = tuple(
        Channel(
            name=f"cam_t.{axis}",
            role=ChannelRole.HEAD,
            space=ChannelSpace.CAMERA_TRANSLATION_ABS,
            origin=SignalOrigin.ESTIMATED,
            is_delta=False,
            is_physical=True,
            unit=None,
            metric_convertible=False,
            frame=ReferenceFrame.WORLD,
            group="cam_t",
        )
        for axis in ("x", "y", "z")
    )
    rotation = tuple(
        Channel(
            name=f"cam_q.{axis}",
            role=ChannelRole.HEAD,
            space=ChannelSpace.CAMERA_ROTATION_ABS,
            origin=SignalOrigin.ESTIMATED,
            is_delta=False,
            is_physical=True,
            unit=None,
            metric_convertible=False,
            frame=ReferenceFrame.WORLD,
            rotation=RotationSpec(repr=RotationRepr.QUAT_WXYZ),
            group="cam_q",
            min=-1.0,
            max=1.0,
        )
        for axis in ("w", "x", "y", "z")
    )
    return translation + rotation


def flag_channel(name: str = "flag.terminate_episode") -> Channel:
    """Source C's terminate flag: not a physical quantity, and must stay out of statistics."""
    return Channel(
        name=name,
        role=ChannelRole.CONTROL_FLAG,
        space=ChannelSpace.FLAG,
        origin=SignalOrigin.MEASURED,
        is_delta=False,
        is_physical=False,
        metric_convertible=False,
        min=0.0,
        max=1.0,
    )


def spec(
    is_command: bool = True,
    level: SignalLevel = SignalLevel.PER_FRAME_CONTINUOUS,
    channels: Sequence[Channel] | None = None,
) -> SignalSpec:
    per_frame = level in (SignalLevel.PER_FRAME_CONTINUOUS, SignalLevel.PER_FRAME_DISCRETE)
    if channels is None:
        channels = xy_channels() if per_frame else ()
    return SignalSpec(is_command=is_command, level=level, channels=tuple(channels))


def frames(n: int = 4, dt: float = 0.1) -> FrameTable:
    t = np.arange(n, dtype=np.float64) * dt
    values = np.arange(n, dtype=np.float64)
    return FrameTable(
        columns={
            "t": t,
            "action.ee.x": values,
            "action.ee.y": values,
            "state.ee.x": values,
            "state.ee.y": values,
        }
    )


def provenance(timestamp_source: str = "real") -> Provenance:
    return Provenance(
        is_original=True,
        timestamp_source=timestamp_source,
        frame_index_source="upstream",
        upstream_revision="main",
        adapter_version="test@1",
    )


def boundary() -> EpisodeBoundary:
    return EpisodeBoundary(
        termination_source=TerminationSource.ENV_RULE,
        end_reason=EndReason.SUCCESS,
        is_truncated=None,
        success=True,
        success_adjudicator=SuccessAdjudicator.SIMULATOR,
    )


def meta(
    n_frames: int = 4,
    upstream_id: str = "episode_000000",
    timestamp_source: str = "real",
    action_level: SignalLevel = SignalLevel.PER_FRAME_CONTINUOUS,
    source_id: str = "pusht",
    action_spec: SignalSpec | None = None,
    state_spec: SignalSpec | None = None,
    capabilities: Capabilities | None = None,
    stream_specs: Mapping[str, SignalSpec] | None = None,
    cameras: Sequence[CameraSpec] = (),
    segment: EpisodeSegment | None = None,
    termination_column: str | None = None,
    raw_frame_columns: Sequence[str] = (),
    fps_nominal: float | None = 10.0,
) -> EpisodeMeta:
    return EpisodeMeta(
        uid=make_uid(source_id, upstream_id),
        source_id=source_id,
        upstream_id=upstream_id,
        embodiment="pusht_planar",
        n_frames=n_frames,
        action_spec=action_spec or spec(is_command=True, level=action_level),
        state_spec=state_spec or spec(is_command=False),
        stream_specs=dict(stream_specs or {}),
        capabilities=capabilities
        or Capabilities(has_action=action_level != SignalLevel.ABSENT, has_state=True),
        provenance=provenance(timestamp_source),
        boundary=boundary(),
        cameras=tuple(cameras),
        segment=segment,
        termination_column=termination_column,
        raw_frame_columns=tuple(raw_frame_columns),
        fps_nominal=fps_nominal,
    )


def pose_meta(n_frames: int = 4) -> EpisodeMeta:
    """An episode shaped like source D: a label for an action, an estimated pose for a state."""
    return meta(
        n_frames=n_frames,
        source_id="epic100",
        action_level=SignalLevel.EPISODE_LABEL,
        state_spec=SignalSpec(
            is_command=False,
            level=SignalLevel.PER_FRAME_CONTINUOUS,
            channels=pose_channels(),
        ),
        capabilities=Capabilities(
            has_action=True, has_state=True, has_camera_pose=True, has_rgb=False
        ),
    )


def pose_frames(registered: Sequence[bool], dt: float = 0.1) -> FrameTable:
    """A pose table where unregistered frames are NaN — never 0, which is a place."""
    n = len(registered)
    values = np.where(np.asarray(registered), 0.5, np.nan)
    columns: dict[str, np.ndarray] = {"t": np.arange(n, dtype=np.float64) * dt}
    for channel in pose_channels():
        columns[f"state.{channel.name}"] = values.astype(np.float64)
    return FrameTable(columns=columns)
