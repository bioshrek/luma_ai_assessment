"""The rule catalogue. One module per rule; each is a pure function of frames + metadata."""

from __future__ import annotations

from rdp.domain.qc.rules.action_jerk import ActionJerk
from rdp.domain.qc.rules.action_range import ActionRange
from rdp.domain.qc.rules.fps_drift import FpsDrift
from rdp.domain.qc.rules.gripper_stuck import GripperStuck
from rdp.domain.qc.rules.pose_coverage import PoseCoverage
from rdp.domain.qc.rules.segment_bounds import SegmentBounds
from rdp.domain.qc.rules.state_action_echo import StateActionEcho
from rdp.domain.qc.rules.static_episode import StaticEpisode
from rdp.domain.qc.rules.termination_consistency import TerminationConsistency
from rdp.domain.qc.rules.ts_monotonic import TsMonotonic
from rdp.domain.qc.rules.video_frame_mismatch import VideoFrameMismatch

__all__ = [
    "ActionJerk",
    "ActionRange",
    "FpsDrift",
    "GripperStuck",
    "PoseCoverage",
    "SegmentBounds",
    "StateActionEcho",
    "StaticEpisode",
    "TerminationConsistency",
    "TsMonotonic",
    "VideoFrameMismatch",
]
