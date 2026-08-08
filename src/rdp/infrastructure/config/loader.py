"""YAML configuration -> domain objects.

`sources.local.yaml` overlays `sources.yaml` per `source_id`, which is how machine-specific
paths and mirrors stay out of the committed file.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from rdp.domain.action_spec import Channel, SignalLevel
from rdp.domain.embodiment import Embodiment, EmbodimentRegistry
from rdp.domain.qc.rule import QCRule
from rdp.domain.qc.rules import (
    ActionJerk,
    ActionRange,
    FpsDrift,
    GripperStuck,
    PoseCoverage,
    SegmentBounds,
    StateActionEcho,
    StaticEpisode,
    TerminationConsistency,
    TsMonotonic,
    VideoFrameMismatch,
)
from rdp.domain.source import Source

SOURCE_FIELDS = frozenset(
    {
        "source_id",
        "kind",
        "uri",
        "revision",
        "embodiment",
        "license",
        "max_episodes",
        "with_video",
        "shard_layout_revision",
        "notes",
    }
)

RULE_REGISTRY: dict[str, Callable[..., QCRule]] = {
    "TS_MONOTONIC": TsMonotonic,
    "FPS_DRIFT": FpsDrift,
    "ACTION_RANGE": ActionRange,
    "ACTION_JERK": ActionJerk,
    "STATIC_EPISODE": StaticEpisode,
    "STATE_ACTION_ECHO": StateActionEcho,
    "VIDEO_FRAME_MISMATCH": VideoFrameMismatch,
    "GRIPPER_STUCK": GripperStuck,
    "TERMINATION_CONSISTENCY": TerminationConsistency,
    "POSE_COVERAGE": PoseCoverage,
    "SEGMENT_BOUNDS": SegmentBounds,
}


def _read(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


def load_sources(path: Path, local_path: Path | None = None) -> dict[str, Source]:
    document = _read(path)
    defaults = document.get("defaults", {})
    overlays = _local_overlays(local_path)

    sources: dict[str, Source] = {}
    for entry in document.get("sources", []):
        merged = {**defaults, **entry, **overlays.get(entry["source_id"], {})}
        known = {key: value for key, value in merged.items() if key in SOURCE_FIELDS}
        options = {key: value for key, value in merged.items() if key not in SOURCE_FIELDS}
        sources[known["source_id"]] = Source(**known, options=options)
    return sources


def _local_overlays(local_path: Path | None) -> dict[str, dict[str, Any]]:
    if local_path is None or not local_path.exists():
        return {}
    document = _read(local_path)
    return {entry["source_id"]: entry for entry in document.get("sources", [])}


def load_embodiments(path: Path) -> EmbodimentRegistry:
    document = _read(path)
    embodiments = {}
    for embodiment_id, entry in document.get("embodiments", {}).items():
        action = entry.get("action", {})
        state = entry.get("state", {})
        streams = entry.get("streams", {})
        embodiments[embodiment_id] = Embodiment(
            embodiment_id=embodiment_id,
            description=entry.get("description", ""),
            is_real_robot=entry["is_real_robot"],
            is_teleop=entry["is_teleop"],
            action_level=SignalLevel(action.get("level", SignalLevel.ABSENT)),
            state_level=SignalLevel(state.get("level", SignalLevel.ABSENT)),
            action_channels=_channels(action.get("channels", [])),
            state_channels=_channels(state.get("channels", [])),
            stream_channels={
                stream_id: _channels(stream.get("channels", []))
                for stream_id, stream in streams.items()
            },
        )
    return EmbodimentRegistry(embodiments=embodiments)


def _channels(entries: Sequence[dict[str, Any]]) -> tuple[Channel, ...]:
    return tuple(Channel(**entry) for entry in entries)


def load_rules(path: Path) -> tuple[list[QCRule], str]:
    document = _read(path)
    rules: list[QCRule] = []
    for entry in document.get("rules", []):
        if not entry.get("enabled", True):
            continue
        rule_id = entry["id"]
        if rule_id not in RULE_REGISTRY:
            raise ValueError(f"qc.yaml enables unknown rule {rule_id!r}")
        # Thresholds live in qc.yaml, versioned by `ruleset_version`, never in the rule.
        rules.append(RULE_REGISTRY[rule_id](**entry.get("params", {})))
    return rules, str(document.get("ruleset_version", "0"))
