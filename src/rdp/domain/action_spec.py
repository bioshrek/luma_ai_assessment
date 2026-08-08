"""`SignalSpec` and `Channel` — the shared value objects behind action and state (design §2.2a).

The thesis: unify the structure, never the numbers. Semantics live on the **channel**;
spec-level `space` / `is_delta` / `dim` / `physical_dim` are *derived summaries* and cannot be
set by hand (invariant 9).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from rdp.domain.errors import InvariantViolation


class SignalLevel(StrEnum):
    PER_FRAME_CONTINUOUS = "per_frame_continuous"
    PER_FRAME_DISCRETE = "per_frame_discrete"
    EPISODE_LABEL = "episode_label"
    ABSENT = "absent"


class SpecSpace(StrEnum):
    JOINT_POSITION = "joint_position"
    EE_POSE_ABS = "ee_pose_abs"
    EE_POSE_DELTA = "ee_pose_delta"
    CARTESIAN_2D = "cartesian_2d"
    CAMERA_POSE_ABS = "camera_pose_abs"
    IMU = "imu"
    MIXED = "mixed"
    NONE = "none"
    UNKNOWN = "unknown"


class ChannelRole(StrEnum):
    JOINT = "joint"
    END_EFFECTOR = "end_effector"
    GRIPPER = "gripper"
    BASE = "base"
    HEAD = "head"
    CONTROL_FLAG = "control_flag"
    UNKNOWN = "unknown"


class ChannelSpace(StrEnum):
    JOINT_POSITION = "joint_position"
    EE_TRANSLATION_ABS = "ee_translation_abs"
    EE_TRANSLATION_DELTA = "ee_translation_delta"
    EE_ROTATION_ABS = "ee_rotation_abs"
    EE_ROTATION_DELTA = "ee_rotation_delta"
    CARTESIAN_2D = "cartesian_2d"
    CAMERA_TRANSLATION_ABS = "camera_translation_abs"
    CAMERA_ROTATION_ABS = "camera_rotation_abs"
    IMU_ANGULAR_VELOCITY = "imu_angular_velocity"
    IMU_LINEAR_ACCELERATION = "imu_linear_acceleration"
    GRIPPER = "gripper"
    FLAG = "flag"
    UNKNOWN = "unknown"


class SignalOrigin(StrEnum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    INTERPOLATED = "interpolated"
    ANNOTATED = "annotated"
    SYNTHESIZED = "synthesized"


class ReferenceFrame(StrEnum):
    BASE = "base"
    TOOL = "tool"
    WORLD = "world"
    CAMERA = "camera"
    SENSOR = "sensor"


class Unit(StrEnum):
    RAD = "rad"
    M = "m"
    PX = "px"
    RAD_PER_S = "rad/s"
    M_PER_S2 = "m/s^2"
    NORMALIZED = "normalized"


class ArmId(StrEnum):
    LEFT = "left"
    RIGHT = "right"


class RotationRepr(StrEnum):
    AXIS_ANGLE = "axis_angle"
    ROTVEC = "rotvec"
    EULER_XYZ = "euler_xyz"
    EULER_ZYX = "euler_zyx"
    QUAT_WXYZ = "quat_wxyz"
    UNKNOWN = "unknown"


class RotationCompose(StrEnum):
    PRE = "pre"
    POST = "post"
    UNKNOWN = "unknown"


class SignalClock(StrEnum):
    FRAME = "frame"
    OWN_TIMELINE = "own_timeline"


ROTATION_SPACES = frozenset(
    {
        ChannelSpace.EE_ROTATION_ABS,
        ChannelSpace.EE_ROTATION_DELTA,
        ChannelSpace.CAMERA_ROTATION_ABS,
    }
)

# Channel space -> the spec-level summary it rolls up to. A gripper has no spec-level space of
# its own, so it always summarises as "mixed" (the truth is on the channel; invariant 9).
_SPEC_SPACE_OF: dict[ChannelSpace, SpecSpace] = {
    ChannelSpace.JOINT_POSITION: SpecSpace.JOINT_POSITION,
    ChannelSpace.EE_TRANSLATION_ABS: SpecSpace.EE_POSE_ABS,
    ChannelSpace.EE_ROTATION_ABS: SpecSpace.EE_POSE_ABS,
    ChannelSpace.EE_TRANSLATION_DELTA: SpecSpace.EE_POSE_DELTA,
    ChannelSpace.EE_ROTATION_DELTA: SpecSpace.EE_POSE_DELTA,
    ChannelSpace.CARTESIAN_2D: SpecSpace.CARTESIAN_2D,
    ChannelSpace.CAMERA_TRANSLATION_ABS: SpecSpace.CAMERA_POSE_ABS,
    ChannelSpace.CAMERA_ROTATION_ABS: SpecSpace.CAMERA_POSE_ABS,
    ChannelSpace.IMU_ANGULAR_VELOCITY: SpecSpace.IMU,
    ChannelSpace.IMU_LINEAR_ACCELERATION: SpecSpace.IMU,
    ChannelSpace.GRIPPER: SpecSpace.MIXED,
    ChannelSpace.FLAG: SpecSpace.UNKNOWN,
    ChannelSpace.UNKNOWN: SpecSpace.UNKNOWN,
}


class RotationSpec(BaseModel):
    """Three radians are unreadable without knowing the representation (design §2.2a)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repr: RotationRepr
    compose: RotationCompose | None = None


class GripperInverse(BaseModel):
    """Parameters that undo the `0=closed, 1=open` normalization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scale: float
    offset: float


class GripperSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    convention: str = "0=closed,1=open"
    original_convention: str
    inverse: GripperInverse


class Channel(BaseModel):
    """What column *i* actually means. This — not the spec — is the source of truth."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    role: ChannelRole
    space: ChannelSpace
    origin: SignalOrigin
    is_delta: bool
    is_physical: bool
    unit: Unit | None = None
    metric_convertible: bool = False
    group: str | None = None
    frame: ReferenceFrame | None = None
    arm_id: ArmId | None = None
    min: float | None = None
    max: float | None = None
    rotation: RotationSpec | None = None
    gripper: GripperSpec | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        # Invariant 8: gripper metadata exists exactly when the channel is a gripper.
        if (self.role is ChannelRole.GRIPPER) != (self.gripper is not None):
            raise InvariantViolation(
                f"channel {self.name!r}: role=gripper <=> channel.gripper is non-null (invariant 8)"
            )
        # Invariant 10: rotation metadata exists exactly for rotation channels.
        if (self.space in ROTATION_SPACES) != (self.rotation is not None):
            raise InvariantViolation(
                f"channel {self.name!r}: rotation space <=> rotation is non-null (invariant 10)"
            )
        if self.min is not None and self.max is not None and self.min > self.max:
            raise InvariantViolation(f"channel {self.name!r}: min > max")
        if "." in self.name and self.name.startswith(("action.", "state.", "raw.")):
            raise InvariantViolation(
                f"channel {self.name!r} must not carry a column prefix; FrameTable adds it"
            )
        return self


class SignalSpec(BaseModel):
    """Shared by action and state; `is_command` is the only difference (design §2.2b')."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    is_command: bool
    level: SignalLevel
    channels: tuple[Channel, ...] = ()
    clock: SignalClock = SignalClock.FRAME

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dim(self) -> int:
        return len(self.channels)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def physical_dim(self) -> int:
        return len(self.physical_channels)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def space(self) -> SpecSpace:
        """Derived summary over physical channels only — never set by hand (invariant 9)."""
        summaries = {_SPEC_SPACE_OF[c.space] for c in self.physical_channels}
        if not summaries:
            return SpecSpace.NONE
        if len(summaries) == 1:
            return next(iter(summaries))
        return SpecSpace.MIXED

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_delta(self) -> bool:
        """Derived summary. C proves this cannot be trusted at spec level; see invariant 9."""
        return any(c.is_delta for c in self.physical_channels)

    @property
    def physical_channels(self) -> tuple[Channel, ...]:
        return tuple(c for c in self.channels if c.is_physical)

    @property
    def is_per_frame(self) -> bool:
        return self.level in (SignalLevel.PER_FRAME_CONTINUOUS, SignalLevel.PER_FRAME_DISCRETE)

    @property
    def column_prefix(self) -> str:
        return "action" if self.is_command else "state"

    def column_names(self) -> tuple[str, ...]:
        prefix = self.column_prefix
        return tuple(f"{prefix}.{c.name}" for c in self.channels)

    @model_validator(mode="after")
    def _check(self) -> Self:
        names = [c.name for c in self.channels]
        if len(set(names)) != len(names):
            raise InvariantViolation(f"duplicate channel names in the {self.column_prefix} spec")
        # Invariant 3: non per-frame levels store no columns at all.
        if not self.is_per_frame and self.channels:
            raise InvariantViolation(
                f"level={self.level} implies dim == 0, got {len(self.channels)} (invariant 3)"
            )
        if self.is_per_frame and not self.channels:
            raise InvariantViolation(f"level={self.level} requires at least one channel")
        _check_groups(self.channels)
        return self


def _check_groups(channels: tuple[Channel, ...]) -> None:
    """Invariant 16: channels sharing a `group` agree on space / frame / unit / origin."""
    groups: dict[str, list[Channel]] = {}
    for channel in channels:
        if channel.group is not None:
            groups.setdefault(channel.group, []).append(channel)
    for group, members in groups.items():
        head = members[0]
        for member in members[1:]:
            for attr in ("space", "frame", "unit", "origin"):
                if getattr(member, attr) != getattr(head, attr):
                    raise InvariantViolation(
                        f"group {group!r}: {member.name!r} disagrees on {attr} (invariant 16)"
                    )
