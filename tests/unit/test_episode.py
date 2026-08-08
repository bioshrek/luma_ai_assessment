"""`EpisodeMeta`, `CanonicalEpisode` and the `Episode` aggregate root."""

from __future__ import annotations

import numpy as np
import pytest
from tests.factories import frames, meta, provenance, spec

from rdp.domain.action_spec import SignalLevel
from rdp.domain.boundary import EndReason, EpisodeBoundary, SuccessAdjudicator, TerminationSource
from rdp.domain.capabilities import Capabilities
from rdp.domain.episode import CanonicalEpisode, Episode, EpisodeMeta
from rdp.domain.errors import InvariantViolation
from rdp.domain.frames import FrameTable
from rdp.domain.provenance import Provenance, synthesized_at
from rdp.domain.qc.rule import EpisodeVerdict
from rdp.domain.stage import IngestionStage


def test_uid_is_source_and_upstream_id() -> None:
    assert meta().uid == "pusht:episode_000000"
    payload = _strip_derived(meta().model_dump())
    payload["uid"] = "something_else"
    with pytest.raises(InvariantViolation, match="uid"):
        EpisodeMeta.model_validate(payload)


def test_absent_level_and_capability_must_agree() -> None:
    """Invariant 3, at the episode level: 'absent' <=> the capability is False."""
    base = meta().model_dump()
    base["capabilities"] = Capabilities(has_action=True, has_state=True).model_dump()
    base["action_spec"] = spec(is_command=True, level=SignalLevel.ABSENT).model_dump()
    with pytest.raises(InvariantViolation, match="invariant 3"):
        EpisodeMeta.model_validate(_strip_derived(base))


def test_has_video_requires_a_sidecar_camera() -> None:
    base = meta().model_dump()
    base["capabilities"] = Capabilities(
        has_action=True, has_state=True, has_video=True
    ).model_dump()
    with pytest.raises(InvariantViolation, match="mp4_sidecar"):
        EpisodeMeta.model_validate(_strip_derived(base))


def test_synthesized_timestamps_are_not_real_timestamps() -> None:
    assert provenance().has_real_timestamps is True
    assert provenance(synthesized_at(10.0)).has_real_timestamps is False
    with pytest.raises(InvariantViolation, match="timestamp_source"):
        Provenance(
            is_original=True,
            timestamp_source="probably_fine",
            frame_index_source="upstream",
            upstream_revision="main",
            adapter_version="test@1",
        )


def test_frame_index_source_must_carry_the_fps_it_was_derived_with() -> None:
    with pytest.raises(InvariantViolation, match="invariant 15"):
        Provenance(
            is_original=True,
            timestamp_source="real",
            frame_index_source="derived",
            upstream_revision="main",
            adapter_version="test@1",
        )


def test_truncated_episode_cannot_also_be_a_success() -> None:
    with pytest.raises(InvariantViolation, match="invariant 7"):
        EpisodeBoundary(
            termination_source=TerminationSource.ENV_RULE,
            end_reason=EndReason.SUCCESS,
            is_truncated=True,
            success=True,
            success_adjudicator=SuccessAdjudicator.SIMULATOR,
        )


def test_no_adjudicator_means_success_is_unknown_not_false() -> None:
    with pytest.raises(InvariantViolation, match="invariant 14"):
        EpisodeBoundary(
            termination_source=TerminationSource.ANNOTATOR,
            end_reason=EndReason.ANNOTATION_BOUND,
            is_truncated=None,
            success=False,
            success_adjudicator=SuccessAdjudicator.NONE,
        )


def test_canonical_episode_requires_declared_columns_to_exist() -> None:
    incomplete = FrameTable(columns={"t": frames().t, "action.ee.x": frames().t})
    with pytest.raises(InvariantViolation, match="invariant 2"):
        CanonicalEpisode(meta=meta(), frames=incomplete)


def test_canonical_episode_requires_the_row_count_to_match_meta() -> None:
    with pytest.raises(InvariantViolation, match="n_frames"):
        CanonicalEpisode(meta=meta(n_frames=99), frames=frames())


def test_content_hash_ignores_the_order_columns_were_inserted_in() -> None:
    a = CanonicalEpisode(meta=meta(), frames=frames())
    shuffled = FrameTable(columns=dict(reversed(list(frames().columns.items()))))
    assert a.content_hash() == CanonicalEpisode(meta=meta(), frames=shuffled).content_hash()


def test_content_hash_tracks_the_numbers() -> None:
    a = CanonicalEpisode(meta=meta(), frames=frames())
    columns = dict(frames().columns)
    columns["state.ee.y"] = np.zeros(4)
    b = CanonicalEpisode(meta=meta(), frames=FrameTable(columns=columns))
    assert a.content_hash() != b.content_hash()


def test_episode_lifecycle_records_the_run_that_touched_it() -> None:
    episode = Episode.discovered(
        source_id="pusht", upstream_id="episode_000000", run_id="r1", now="t0"
    )
    assert episode.stage is IngestionStage.DISCOVERED
    episode = episode.fetched(run_id="r2", now="t1")
    assert (episode.stage, episode.first_seen_run, episode.last_update_run) == (
        IngestionStage.FETCHED,
        "r1",
        "r2",
    )


def test_normalized_stage_requires_the_pointers_that_make_it_reproducible() -> None:
    with pytest.raises(InvariantViolation, match="requires meta"):
        Episode(
            uid="pusht:e0",
            source_id="pusht",
            upstream_id="e0",
            stage=IngestionStage.NORMALIZED,
        )


def test_an_episode_cannot_be_committed_while_qc_is_pending() -> None:
    with pytest.raises(InvariantViolation, match="QC is PENDING"):
        Episode(
            uid="pusht:e0",
            source_id="pusht",
            upstream_id="e0",
            stage=IngestionStage.COMMITTED,
            meta=meta(),
            frames_path="pusht/e0",
            content_hash="abc",
            qc_verdict=EpisodeVerdict.PENDING,
        )


def test_failure_is_recorded_with_its_error_and_can_be_retried() -> None:
    episode = Episode.discovered(
        source_id="pusht", upstream_id="e0", run_id="r1", now="t0"
    ).failed(error="boom", run_id="r1", now="t1")
    assert episode.stage is IngestionStage.FAILED
    assert episode.last_error == "boom"
    assert episode.stage.reset_to(IngestionStage.DISCOVERED, "retry") is IngestionStage.DISCOVERED


def _strip_derived(payload: dict[str, object]) -> dict[str, object]:
    """`model_dump()` emits computed fields; `extra='forbid'` rejects them on the way back in."""
    for key in ("action_spec", "state_spec"):
        spec_payload = dict(payload[key])  # type: ignore[arg-type]
        for derived in ("dim", "physical_dim", "space", "is_delta"):
            spec_payload.pop(derived, None)
        payload[key] = spec_payload
    return payload
