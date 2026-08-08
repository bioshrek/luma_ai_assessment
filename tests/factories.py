"""Builders for valid domain objects, so each test states only what it is about."""

from __future__ import annotations

import numpy as np

from rdp.domain.action_spec import (
    Channel,
    ChannelRole,
    ChannelSpace,
    ReferenceFrame,
    SignalLevel,
    SignalOrigin,
    SignalSpec,
    Unit,
)
from rdp.domain.boundary import EndReason, EpisodeBoundary, SuccessAdjudicator, TerminationSource
from rdp.domain.capabilities import Capabilities
from rdp.domain.episode import EpisodeMeta, make_uid
from rdp.domain.frames import FrameTable
from rdp.domain.provenance import Provenance


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


def spec(
    is_command: bool = True, level: SignalLevel = SignalLevel.PER_FRAME_CONTINUOUS
) -> SignalSpec:
    per_frame = level in (SignalLevel.PER_FRAME_CONTINUOUS, SignalLevel.PER_FRAME_DISCRETE)
    channels = xy_channels() if per_frame else ()
    return SignalSpec(is_command=is_command, level=level, channels=channels)


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
) -> EpisodeMeta:
    return EpisodeMeta(
        uid=make_uid("pusht", upstream_id),
        source_id="pusht",
        upstream_id=upstream_id,
        embodiment="pusht_planar",
        n_frames=n_frames,
        action_spec=spec(is_command=True, level=action_level),
        state_spec=spec(is_command=False),
        capabilities=Capabilities(
            has_action=action_level != SignalLevel.ABSENT, has_state=True
        ),
        provenance=provenance(timestamp_source),
        boundary=boundary(),
        fps_nominal=10.0,
    )
