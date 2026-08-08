"""`SignalSpec` / `Channel`: derived summaries (invariant 9) and per-channel invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.factories import spec, xy_channels

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
    SpecSpace,
    Unit,
)
from rdp.domain.errors import InvariantViolation


def _channel(name: str, **overrides: object) -> Channel:
    base: dict[str, object] = {
        "name": name,
        "role": ChannelRole.END_EFFECTOR,
        "space": ChannelSpace.EE_TRANSLATION_DELTA,
        "origin": SignalOrigin.MEASURED,
        "is_delta": True,
        "is_physical": True,
        "unit": Unit.M,
        "frame": ReferenceFrame.BASE,
    }
    return Channel(**{**base, **overrides})  # type: ignore[arg-type]


def test_dim_and_space_are_derived_not_declared() -> None:
    s = spec()
    assert (s.dim, s.physical_dim) == (2, 2)
    assert s.space is SpecSpace.CARTESIAN_2D
    assert s.is_delta is False
    with pytest.raises(ValidationError):
        # extra="forbid": a derived summary cannot be asserted by hand (invariant 9)
        SignalSpec(
            is_command=True,
            level=SignalLevel.PER_FRAME_CONTINUOUS,
            channels=xy_channels(),
            dim=7,  # type: ignore[call-arg]
        )


def test_non_physical_channels_are_excluded_from_the_summary() -> None:
    """Source C's `terminate_episode` must not turn the action space into 'mixed'."""
    flag = _channel(
        "terminate_episode",
        role=ChannelRole.CONTROL_FLAG,
        space=ChannelSpace.FLAG,
        is_delta=False,
        is_physical=False,
        unit=None,
        frame=None,
    )
    s = SignalSpec(
        is_command=True,
        level=SignalLevel.PER_FRAME_CONTINUOUS,
        channels=(_channel("ee.dx"), flag),
    )
    assert (s.dim, s.physical_dim) == (2, 1)
    assert s.space is SpecSpace.EE_POSE_DELTA


def test_column_names_carry_the_signal_prefix() -> None:
    assert spec(is_command=True).column_names() == ("action.ee.x", "action.ee.y")
    assert spec(is_command=False).column_names() == ("state.ee.x", "state.ee.y")


def test_channel_name_must_not_carry_a_column_prefix() -> None:
    with pytest.raises(InvariantViolation, match="column prefix"):
        _channel("action.ee.dx")


def test_gripper_metadata_is_required_exactly_for_gripper_channels() -> None:
    with pytest.raises(InvariantViolation, match="invariant 8"):
        _channel("gripper", role=ChannelRole.GRIPPER, space=ChannelSpace.GRIPPER)
    ok = _channel(
        "gripper",
        role=ChannelRole.GRIPPER,
        space=ChannelSpace.GRIPPER,
        unit=Unit.NORMALIZED,
        gripper=GripperSpec(
            original_convention="0=open,1=closed",
            inverse=GripperInverse(scale=-1.0, offset=1.0),
        ),
    )
    assert ok.gripper is not None


def test_rotation_metadata_is_required_exactly_for_rotation_channels() -> None:
    with pytest.raises(InvariantViolation, match="invariant 10"):
        _channel("ee.drx", space=ChannelSpace.EE_ROTATION_DELTA, unit=Unit.RAD)
    with pytest.raises(InvariantViolation, match="invariant 10"):
        _channel("ee.dx", rotation=RotationSpec(repr=RotationRepr.AXIS_ANGLE))


def test_absent_level_forbids_channels_and_per_frame_requires_them() -> None:
    with pytest.raises(InvariantViolation, match="invariant 3"):
        SignalSpec(is_command=True, level=SignalLevel.ABSENT, channels=xy_channels())
    with pytest.raises(InvariantViolation, match="requires at least one channel"):
        SignalSpec(is_command=True, level=SignalLevel.PER_FRAME_CONTINUOUS, channels=())


def test_channels_in_one_group_must_agree_on_unit_and_frame() -> None:
    with pytest.raises(InvariantViolation, match="invariant 16"):
        SignalSpec(
            is_command=True,
            level=SignalLevel.PER_FRAME_CONTINUOUS,
            channels=(
                _channel("ee.dx", group="ee_xyz"),
                _channel("ee.dy", group="ee_xyz", unit=Unit.PX),
            ),
        )
