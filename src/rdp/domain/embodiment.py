"""`Embodiment` — the registry that asserts what upstream channels actually mean.

Upstream field names are not trusted: pusht calls its two channels `motor_0` / `motor_1` and
they are neither motors nor joints. The truth is declared here (loaded from
`config/embodiments.yaml`) and applied by the adapter.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from rdp.domain.action_spec import Channel, SignalClock, SignalLevel, SignalSpec
from rdp.domain.errors import InvariantViolation


class Embodiment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    embodiment_id: str = Field(min_length=1)
    description: str
    is_real_robot: bool
    is_teleop: bool
    action_level: SignalLevel
    state_level: SignalLevel
    action_channels: tuple[Channel, ...] = ()
    state_channels: tuple[Channel, ...] = ()
    stream_channels: dict[str, tuple[Channel, ...]] = Field(default_factory=dict)
    """Signals on their own clock — D's 195 Hz IMU against a 50 fps video (invariant 17)."""

    def action_spec(self) -> SignalSpec:
        return SignalSpec(is_command=True, level=self.action_level, channels=self.action_channels)

    def state_spec(self) -> SignalSpec:
        return SignalSpec(is_command=False, level=self.state_level, channels=self.state_channels)

    def stream_spec(self, stream_id: str) -> SignalSpec:
        try:
            channels = self.stream_channels[stream_id]
        except KeyError as exc:
            raise InvariantViolation(
                f"{self.embodiment_id}: no stream {stream_id!r} in config/embodiments.yaml"
            ) from exc
        return SignalSpec(
            is_command=False,
            level=SignalLevel.PER_FRAME_CONTINUOUS,
            channels=channels,
            clock=SignalClock.OWN_TIMELINE,
        )

    def assert_width(self, *, action_dim: int, state_dim: int) -> None:
        """Guard against an upstream layout change silently re-meaning every column."""
        if action_dim != len(self.action_channels):
            raise InvariantViolation(
                f"{self.embodiment_id}: upstream action width {action_dim} != "
                f"{len(self.action_channels)} declared channels"
            )
        if state_dim != len(self.state_channels):
            raise InvariantViolation(
                f"{self.embodiment_id}: upstream state width {state_dim} != "
                f"{len(self.state_channels)} declared channels"
            )


class EmbodimentRegistry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    embodiments: dict[str, Embodiment]

    def get(self, embodiment_id: str) -> Embodiment:
        try:
            return self.embodiments[embodiment_id]
        except KeyError as exc:
            raise InvariantViolation(
                f"unknown embodiment {embodiment_id!r}; declare it in config/embodiments.yaml"
            ) from exc
