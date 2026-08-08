"""`Provenance` — which numbers are upstream facts and which we computed (design §2.2f).

Not documentation: `timestamp_source` gates the timestamp QC rules, and `signal_origin` drives
the severity downgrade in invariant 13.
"""

from __future__ import annotations

import re
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rdp.domain.action_spec import SignalOrigin
from rdp.domain.errors import InvariantViolation

REAL_TIMESTAMP = "real"
ANNOTATION_SECONDS = "annotation_seconds"
_SYNTHESIZED = re.compile(r"^synthesized@[0-9]+(\.[0-9]+)?Hz$")
_DERIVED_INDEX = re.compile(r"^derived_from_seconds@[0-9]+(\.[0-9]+)?$")


def synthesized_at(hz: float) -> str:
    """Build a legal `timestamp_source` for a clock we invented from a declared rate."""
    text = f"{hz:g}"
    return f"synthesized@{text}Hz"


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    is_original: bool
    timestamp_source: str
    frame_index_source: str
    upstream_revision: str
    adapter_version: str
    signal_origin: dict[str, SignalOrigin] = Field(default_factory=dict)
    transforms: tuple[dict[str, Any], ...] = ()
    """Every lossy transform we applied, including dropped channels."""
    mirrors: tuple[dict[str, Any], ...] = ()

    @property
    def has_real_timestamps(self) -> bool:
        return self.timestamp_source == REAL_TIMESTAMP

    @model_validator(mode="after")
    def _check(self) -> Self:
        known = (REAL_TIMESTAMP, ANNOTATION_SECONDS)
        if self.timestamp_source not in known and not _SYNTHESIZED.match(self.timestamp_source):
            raise InvariantViolation(
                f"timestamp_source {self.timestamp_source!r} must be 'real', "
                "'annotation_seconds', or 'synthesized@<hz>Hz'"
            )
        # Invariant 15: a derived quantity must carry the parameter it was derived with.
        if self.frame_index_source != "upstream" and not _DERIVED_INDEX.match(
            self.frame_index_source
        ):
            raise InvariantViolation(
                f"frame_index_source {self.frame_index_source!r} must be 'upstream' or "
                "'derived_from_seconds@<fps>' (invariant 15)"
            )
        return self
