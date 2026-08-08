"""The unified staleness predicate (design §5).

"Upstream changed the data" and "we changed the schema, the adapter or the thresholds" are the
same question — *is what we hold still what our current code would produce?* — so they share one
detection path and one re-run path. That is why schema evolution here needs no migration script:
a schema change is just another round of incremental ingestion (design §8.7).

The answer is not a boolean but *how far back to rewind*, because the stages are not equally
expensive. A ruleset edit must not re-download 50 GB.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rdp.domain.episode import Episode
from rdp.domain.stage import IngestionStage


class Staleness(StrEnum):
    FRESH = "FRESH"
    REDO_NORMALIZE = "REDO_NORMALIZE"
    REDO_QC = "REDO_QC"

    @property
    def rewind_to(self) -> IngestionStage:
        """The stage a stale episode is reset to. `FRESH` has none."""
        return _REWIND[self]


_REWIND = {
    # Raw bytes are immutable and already staged, so even a content change rewinds only to
    # FETCHED: `fetch` is the expensive stage and re-running it would buy nothing.
    Staleness.REDO_NORMALIZE: IngestionStage.FETCHED,
    Staleness.REDO_QC: IngestionStage.NORMALIZED,
}


@dataclass(frozen=True)
class PipelineVersions:
    """The half of the staleness tuple that comes from *our* code rather than from upstream."""

    schema_version: str
    adapter_version: str
    ruleset_version: str


def assess(
    episode: Episode,
    current: PipelineVersions,
    upstream_content_hash: str | None = None,
) -> Staleness:
    """Compare the recorded `(content_hash, schema, adapter, ruleset)` tuple with the current one.

    `upstream_content_hash` is optional because sources differ in when it can be known: A, B and
    D can hash upstream bytes before normalizing, while C's hash is defined over the *normalized*
    episode and so only exists afterwards (design §5). When it is unknown, a content change is
    caught after re-normalization instead of before it, never missed.
    """
    if episode.content_hash is None or episode.meta is None:
        return Staleness.REDO_NORMALIZE
    if upstream_content_hash is not None and upstream_content_hash != episode.content_hash:
        return Staleness.REDO_NORMALIZE
    if episode.meta.schema_version != current.schema_version:
        return Staleness.REDO_NORMALIZE
    if episode.adapter_version != current.adapter_version:
        return Staleness.REDO_NORMALIZE
    if episode.ruleset_version != current.ruleset_version:
        return Staleness.REDO_QC
    return Staleness.FRESH
