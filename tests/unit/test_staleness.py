"""Staleness is the predicate that decides whether a re-run does anything at all."""

from __future__ import annotations

import pytest
from tests import factories

from rdp.domain.episode import SCHEMA_VERSION, Episode
from rdp.domain.stage import IngestionStage
from rdp.domain.staleness import PipelineVersions, Staleness, assess

# Pinned to whatever the domain currently declares: this suite is about the *comparison*, not
# about any one schema number, and hard-coding one only breaks on every schema bump.
CURRENT = PipelineVersions(
    schema_version=SCHEMA_VERSION, adapter_version="a@1", ruleset_version="r@1"
)


def committed(**overrides: object) -> Episode:
    meta = factories.meta()
    fields: dict[str, object] = {
        "uid": meta.uid,
        "source_id": meta.source_id,
        "upstream_id": meta.upstream_id,
        "stage": IngestionStage.COMMITTED,
        "meta": meta,
        "content_hash": "sha256:abc",
        "frames_path": "fake/episode_000000",
        "adapter_version": "a@1",
        "ruleset_version": "r@1",
        "qc_verdict": "PASS",
    }
    fields.update(overrides)
    return Episode(**fields)  # type: ignore[arg-type]


def test_an_unchanged_episode_is_fresh() -> None:
    assert assess(committed(), CURRENT) is Staleness.FRESH


def test_a_new_ruleset_rewinds_only_to_normalized() -> None:
    """The whole point of returning a stage: editing a threshold must not re-download data."""
    stale = assess(committed(ruleset_version="r@0"), CURRENT)
    assert stale is Staleness.REDO_QC
    assert stale.rewind_to is IngestionStage.NORMALIZED


def test_a_new_adapter_rewinds_to_fetched_not_to_discovered() -> None:
    """Raw bytes are immutable, so re-fetching them could only ever waste bandwidth."""
    stale = assess(committed(adapter_version="a@0"), CURRENT)
    assert stale is Staleness.REDO_NORMALIZE
    assert stale.rewind_to is IngestionStage.FETCHED


def test_a_new_schema_version_renormalizes() -> None:
    """Schema evolution is incremental ingestion, not a migration script (design §8.7)."""
    meta = factories.meta().model_copy(update={"schema_version": "0.9"})
    assert assess(committed(meta=meta), CURRENT) is Staleness.REDO_NORMALIZE


def test_changed_upstream_content_renormalizes() -> None:
    assert assess(committed(), CURRENT, upstream_content_hash="sha256:zzz") is (
        Staleness.REDO_NORMALIZE
    )


def test_an_unknown_upstream_hash_is_not_treated_as_a_change() -> None:
    """Source C cannot hash upstream before normalizing; that must not force a re-run."""
    assert assess(committed(), CURRENT, upstream_content_hash=None) is Staleness.FRESH


@pytest.mark.parametrize("stage", [IngestionStage.DISCOVERED, IngestionStage.FETCHED])
def test_an_episode_with_no_identity_yet_is_always_stale(stage: IngestionStage) -> None:
    """Without a hash or metadata there is nothing to compare, so trusting it is not an option."""
    episode = Episode(
        uid="fake:e0", source_id="fake", upstream_id="e0", stage=stage, adapter_version="a@1"
    )
    assert assess(episode, CURRENT) is Staleness.REDO_NORMALIZE


def test_fresh_has_no_rewind_target() -> None:
    with pytest.raises(KeyError):
        _ = Staleness.FRESH.rewind_to
