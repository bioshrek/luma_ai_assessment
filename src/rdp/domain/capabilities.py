"""`Capabilities` — what this **episode** has, hence which QC rules may run (design §2.2e).

Per episode, not per source: only source D can falsify the alternative, but the placement is
decided here from day one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Capabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    has_action: bool = False
    has_state: bool = False
    has_gripper: bool = False
    has_rgb: bool = False
    """Any RGB imagery exists upstream, including frames inlined in the records."""
    has_video: bool = False
    """A decodable standalone video file exists locally. This is what QC rules depend on."""
    has_language: bool = False
    has_reward: bool = False
    has_depth: bool = False
    has_imu: bool = False
    has_camera_pose: bool = False
    has_termination_signal: bool = False
    is_real_robot: bool = False
    is_teleop: bool = False

    def has(self, name: str) -> bool:
        value = getattr(self, name, None)
        if not isinstance(value, bool):
            raise AttributeError(f"unknown capability {name!r}")
        return value
