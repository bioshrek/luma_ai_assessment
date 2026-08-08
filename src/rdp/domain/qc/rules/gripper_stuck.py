"""`GRIPPER_STUCK` — the gripper never actuates in a demonstration that should have used it.

**The trap M0 uncovered (design §3, ADR 003).** The obvious rule — "the gripper column has one
unique value" — is right for B, whose gripper is an absolute opening, and catastrophically wrong
for C, whose gripper is a *ternary change command* where `0` is the normal resting value.
Written against B's convention it would fire on essentially every C episode, because "no change
this step" is what a gripper spends most of its time doing.

So the rule reads `channel.is_delta` before it reads a single value, and asks a different
question of each kind:

- **absolute** — how many distinct openings did it take? One means stuck.
- **delta** — did the *cumulative* command ever leave zero? Never means the gripper was never
  commanded at all, which on 5 of C's 12 episodes is exactly what happened.

REVIEW, not FAIL, and only on episodes long enough for it to mean something: a short episode may
legitimately not reach the grasp.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np

from rdp.domain.action_spec import ChannelRole, SignalLevel
from rdp.domain.frames import FrameTable
from rdp.domain.qc.rule import QCEpisodeView, RuleResult, Severity, Verdict

RULE_ID = "GRIPPER_STUCK"


def _required_levels() -> Mapping[str, SignalLevel]:
    return {"action": SignalLevel.PER_FRAME_CONTINUOUS}


@dataclass(frozen=True)
class GripperStuck:
    min_frames: int = 50
    rule_id: str = RULE_ID
    severity: Severity = Severity.REVIEW
    required_capabilities: frozenset[str] = frozenset({"has_action", "has_gripper"})
    required_levels: Mapping[str, SignalLevel] = field(default_factory=_required_levels)
    requires_real_timestamps: bool = False

    def evaluate(self, frames: FrameTable, meta: QCEpisodeView) -> RuleResult:
        spec = meta.spec_of("action")
        grippers = [c for c in spec.channels if c.role is ChannelRole.GRIPPER]
        if not grippers:
            raise ValueError("has_gripper is set but the action spec declares no gripper channel")
        if frames.n_frames <= self.min_frames:
            return RuleResult(
                self.rule_id,
                Verdict.SKIPPED,
                {"n_gripper_channels": float(len(grippers))},
                f"{frames.n_frames} frames is too short to expect an actuation "
                f"(needs > {self.min_frames})",
            )

        stuck: list[str] = []
        worst_travel = float("inf")
        fewest_values = float("inf")
        for channel in grippers:
            values = np.asarray(
                frames.column(f"{spec.column_prefix}.{channel.name}"), dtype=np.float64
            )
            values = values[np.isfinite(values)]
            if values.size == 0:
                continue
            if channel.is_delta:
                # The command is a change; what matters is whether the *position* it implies
                # ever moves. All-zero commands mean the gripper was never told to do anything.
                travel = float(np.abs(values).sum())
                worst_travel = min(worst_travel, travel)
                if travel == 0.0:
                    stuck.append(channel.name)
            else:
                distinct = float(np.unique(values).size)
                fewest_values = min(fewest_values, distinct)
                if distinct <= 1:
                    stuck.append(channel.name)

        metrics = {"n_gripper_channels": float(len(grippers))}
        if np.isfinite(fewest_values):
            metrics["min_unique_values"] = fewest_values
        if np.isfinite(worst_travel):
            metrics["min_gripper_travel"] = worst_travel
        if stuck:
            return RuleResult(
                self.rule_id,
                Verdict.REVIEW,
                metrics,
                f"gripper never actuates over {frames.n_frames} frames: "
                f"{', '.join(sorted(stuck))}",
            )
        return RuleResult(self.rule_id, Verdict.PASS, metrics, "every gripper channel actuates")
