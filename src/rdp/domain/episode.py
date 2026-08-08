"""`Episode` — the aggregate root — plus `EpisodeMeta` and `CanonicalEpisode` (design §8.1).

- `EpisodeMeta` is everything about an episode except its numbers.
- `CanonicalEpisode` is `EpisodeMeta` + `FrameTable`: the immutable output of `normalize()`, and
  the only thing that crosses a bounded-context boundary.
- `Episode` is the catalog view: identity, stage, verdict and pointers. Before an episode is
  normalized we know only its identity, so `meta` is None until then.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rdp.domain.action_spec import SignalClock, SignalLevel, SignalOrigin, SignalSpec
from rdp.domain.boundary import EpisodeBoundary
from rdp.domain.camera import CameraEncoding, CameraSpec
from rdp.domain.capabilities import Capabilities
from rdp.domain.errors import InvariantViolation
from rdp.domain.frames import TIME_COLUMN, FrameTable
from rdp.domain.provenance import Provenance
from rdp.domain.qc.rule import EpisodeVerdict
from rdp.domain.segment import EpisodeSegment
from rdp.domain.stage import IngestionStage
from rdp.domain.stats import ChannelStats

SCHEMA_VERSION = "1.1"


def make_uid(source_id: str, upstream_id: str) -> str:
    return f"{source_id}:{upstream_id}"


class EpisodeMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    uid: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    upstream_id: str = Field(min_length=1)
    embodiment: str = Field(min_length=1)
    n_frames: int = Field(ge=0)
    action_spec: SignalSpec
    state_spec: SignalSpec
    capabilities: Capabilities
    provenance: Provenance
    boundary: EpisodeBoundary
    cameras: tuple[CameraSpec, ...] = ()
    task: str | None = None
    fps_nominal: float | None = None
    fps_effective: float | None = None
    duration_s: float | None = None
    segment: EpisodeSegment | None = None
    """Set only when this episode is an interval of a longer parent recording."""
    termination_column: str | None = None
    """Name of the `raw.*` column carrying the upstream end-of-episode marker, when one survived
    normalization. The rule must not guess the name: pusht says `raw.next.done`, C says
    `raw.is_terminal`, and a source may rename it between revisions."""
    raw_extra: dict[str, Any] = Field(default_factory=dict)
    raw_frame_columns: tuple[str, ...] = ()
    stream_specs: dict[str, SignalSpec] = Field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    @property
    def has_real_timestamps(self) -> bool:
        return self.provenance.has_real_timestamps

    def level_of(self, signal: str) -> SignalLevel:
        return self.spec_of(signal).level

    def origins_of(self, signal: str) -> frozenset[SignalOrigin]:
        """Trustworthiness of the physical channels a rule would read (invariant 13)."""
        return frozenset(c.origin for c in self.spec_of(signal).physical_channels)

    def spec_of(self, signal: str) -> SignalSpec:
        if signal == "action":
            return self.action_spec
        if signal == "state":
            return self.state_spec
        raise InvariantViolation(f"unknown signal {signal!r}")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.uid != make_uid(self.source_id, self.upstream_id):
            raise InvariantViolation(f"uid {self.uid!r} must be '<source_id>:<upstream_id>'")
        if self.action_spec.is_command is not True or self.state_spec.is_command is not False:
            raise InvariantViolation(
                "action_spec must be is_command=True and state_spec is_command=False"
            )
        # Invariant 3: level == "absent" <=> the capability is False.
        for signal, spec, present in (
            ("action", self.action_spec, self.capabilities.has_action),
            ("state", self.state_spec, self.capabilities.has_state),
        ):
            if (spec.level is SignalLevel.ABSENT) == present:
                raise InvariantViolation(
                    f"{signal}: level={spec.level} contradicts has_{signal}={present} (invariant 3)"
                )
        # Invariant 11: only a standalone video file may set has_video.
        if self.capabilities.has_video and not any(
            camera.encoding == CameraEncoding.MP4_SIDECAR for camera in self.cameras
        ):
            raise InvariantViolation(
                "has_video=True requires a camera with encoding='mp4_sidecar'"
            )
        # Invariant 17: a stream exists precisely because it does not share the frame clock.
        for stream_id, spec in self.stream_specs.items():
            if spec.clock is not SignalClock.OWN_TIMELINE:
                raise InvariantViolation(
                    f"stream {stream_id!r} must declare clock='own_timeline' (invariant 17)"
                )
        # Invariants 18 and 19, both stated one-directionally: they constrain what we recorded,
        # never what upstream owes us. A source that has neither is simply silent on both.
        if self.segment is not None and not self.capabilities.is_segment:
            raise InvariantViolation("segment is set but is_segment=False (invariant 18)")
        if self.termination_column is not None:
            if not self.capabilities.has_termination_signal:
                raise InvariantViolation(
                    "termination_column is set but has_termination_signal=False (invariant 19)"
                )
            if self.termination_column not in self.raw_frame_columns:
                raise InvariantViolation(
                    f"termination_column {self.termination_column!r} is not among "
                    f"raw_frame_columns (invariant 19)"
                )
        return self


@dataclass(frozen=True, eq=False)
class CanonicalEpisode:
    """Immutable once built. Specs and stored columns are checked to agree here."""

    meta: EpisodeMeta
    frames: FrameTable
    streams: Mapping[str, FrameTable] = field(default_factory=dict)
    """Own-timeline signals, one table each, keyed as in `meta.stream_specs` (design §2.2h)."""

    def __post_init__(self) -> None:
        meta, frames = self.meta, self.frames
        if frames.n_frames != meta.n_frames:
            raise InvariantViolation(
                f"{meta.uid}: meta.n_frames={meta.n_frames} but frames hold {frames.n_frames} rows"
            )
        if frames.raw_frame_columns != meta.raw_frame_columns:
            raise InvariantViolation(f"{meta.uid}: meta and frames disagree on raw_frame_columns")
        for spec in (meta.action_spec, meta.state_spec):
            self._check_spec_columns(spec)
        self._check_streams()

    def _check_streams(self) -> None:
        meta = self.meta
        if set(self.streams) != set(meta.stream_specs):
            raise InvariantViolation(
                f"{meta.uid}: stream tables {sorted(self.streams)} != declared "
                f"{sorted(meta.stream_specs)}"
            )
        for stream_id, table in self.streams.items():
            spec = meta.stream_specs[stream_id]
            expected = {TIME_COLUMN, *spec.column_names()}
            if set(table.column_names) != expected:
                raise InvariantViolation(
                    f"{meta.uid}: stream {stream_id!r} columns {sorted(table.column_names)} != "
                    f"declared {sorted(expected)} (invariant 2)"
                )
            # Invariant 17: the whole point of a separate table is its own, ordered clock.
            if table.n_frames > 1 and bool((table.t[1:] < table.t[:-1]).any()):
                raise InvariantViolation(
                    f"{meta.uid}: stream {stream_id!r} has a non-monotonic 't' (invariant 17)"
                )

    def _check_spec_columns(self, spec: SignalSpec) -> None:
        expected = spec.column_names()
        prefix = f"{spec.column_prefix}."
        present = tuple(n for n in self.frames.column_names if n.startswith(prefix))
        if spec.clock is SignalClock.OWN_TIMELINE:
            # Invariant 17: own-timeline signals never enter the frame table.
            if present:
                raise InvariantViolation(
                    f"{self.meta.uid}: own_timeline columns must not appear in frames: {present}"
                )
            return
        if not spec.is_per_frame:
            # Invariant 3: not a column of NULLs — no column at all.
            if present:
                raise InvariantViolation(
                    f"{self.meta.uid}: level={spec.level} must store no {prefix} columns, "
                    f"found {present} (invariant 3)"
                )
            return
        # Invariant 2: spec.dim == len(channels) == the stored column width.
        if set(present) != set(expected):
            raise InvariantViolation(
                f"{self.meta.uid}: {spec.column_prefix} columns {sorted(present)} != "
                f"declared {sorted(expected)} (invariant 2)"
            )

    def content_hash(self) -> str:
        """Digest of canonical bytes — the logical content, not the parquet container.

        Every enabled layer contributes: source D's pose lands in the frame table and its IMU in
        a stream, so an episode whose IMU arrived only on the second run must not hash equal to
        the one that had none.
        """
        order = [
            TIME_COLUMN,
            *self.meta.action_spec.column_names(),
            *self.meta.state_spec.column_names(),
        ]
        digest = self.frames.canonical_digest(order)
        if not self.streams:
            return digest
        parts = [digest]
        for stream_id in sorted(self.streams):
            spec = self.meta.stream_specs[stream_id]
            stream_order = [TIME_COLUMN, *spec.column_names()]
            parts.append(f"{stream_id}={self.streams[stream_id].canonical_digest(stream_order)}")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()


class Episode(BaseModel):
    """Aggregate root. Stage transitions go through `IngestionStage`, never through assignment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    uid: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    upstream_id: str = Field(min_length=1)
    stage: IngestionStage
    qc_verdict: EpisodeVerdict = EpisodeVerdict.PENDING
    meta: EpisodeMeta | None = None
    content_hash: str | None = None
    frames_path: str | None = None
    adapter_version: str | None = None
    ruleset_version: str | None = None
    channel_stats: dict[str, ChannelStats] = Field(default_factory=dict)
    last_error: str | None = None
    first_seen_run: str = ""
    last_update_run: str = ""
    updated_at: str = ""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.uid != make_uid(self.source_id, self.upstream_id):
            raise InvariantViolation(f"uid {self.uid!r} must be '<source_id>:<upstream_id>'")
        if self.stage.at_least(IngestionStage.NORMALIZED) and (
            self.meta is None or self.frames_path is None or self.content_hash is None
        ):
            raise InvariantViolation(
                f"{self.uid}: stage {self.stage} requires meta, frames_path and content_hash"
            )
        committed = self.stage.at_least(IngestionStage.COMMITTED)
        if committed and self.qc_verdict is EpisodeVerdict.PENDING:
            raise InvariantViolation(f"{self.uid}: cannot commit an episode whose QC is PENDING")
        return self

    @classmethod
    def discovered(cls, *, source_id: str, upstream_id: str, run_id: str, now: str) -> Episode:
        return cls(
            uid=make_uid(source_id, upstream_id),
            source_id=source_id,
            upstream_id=upstream_id,
            stage=IngestionStage.DISCOVERED,
            first_seen_run=run_id,
            last_update_run=run_id,
            updated_at=now,
        )

    def _touched(self, run_id: str, now: str, **changes: Any) -> Episode:
        return self.model_copy(
            update={"last_update_run": run_id, "updated_at": now, "last_error": None, **changes}
        )

    def fetched(self, *, run_id: str, now: str) -> Episode:
        return self._touched(run_id, now, stage=self.stage.advance_to(IngestionStage.FETCHED))

    def normalized(
        self,
        *,
        meta: EpisodeMeta,
        frames_path: str,
        content_hash: str,
        adapter_version: str,
        channel_stats: dict[str, ChannelStats],
        run_id: str,
        now: str,
    ) -> Episode:
        return self._touched(
            run_id,
            now,
            stage=self.stage.advance_to(IngestionStage.NORMALIZED),
            meta=meta,
            frames_path=frames_path,
            content_hash=content_hash,
            adapter_version=adapter_version,
            channel_stats=channel_stats,
        )

    def qc_done(
        self, *, verdict: EpisodeVerdict, ruleset_version: str, run_id: str, now: str
    ) -> Episode:
        return self._touched(
            run_id,
            now,
            stage=self.stage.advance_to(IngestionStage.QC_DONE),
            qc_verdict=verdict,
            ruleset_version=ruleset_version,
        )

    def committed(self, *, run_id: str, now: str) -> Episode:
        return self._touched(run_id, now, stage=self.stage.advance_to(IngestionStage.COMMITTED))

    def requeue(self, *, stage: IngestionStage, reason: str, run_id: str, now: str) -> Episode:
        """Rewind to an earlier stage so the pipeline redoes it — staleness and recovery both.

        The verdict goes back to PENDING because it was reached by code or thresholds we are
        about to replace; carrying it forward would let a stale conclusion look current.
        """
        return self._touched(
            run_id,
            now,
            stage=self.stage.reset_to(stage, reason=reason),
            qc_verdict=EpisodeVerdict.PENDING,
        )

    def failed(self, *, error: str, run_id: str, now: str) -> Episode:
        return self.model_copy(
            update={
                "stage": self.stage.reset_to(IngestionStage.FAILED, reason=error),
                "last_error": error,
                "last_update_run": run_id,
                "updated_at": now,
            }
        )
