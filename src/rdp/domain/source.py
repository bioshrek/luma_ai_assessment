"""`Source` — one upstream dataset, including the revision it was read at (design §8.1)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Source(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1)
    kind: str
    uri: str
    revision: str = "main"
    embodiment: str
    license: str | None = None
    max_episodes: int | None = None
    with_video: bool = False
    shard_layout_revision: str | None = None
    notes: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    """Source-kind specific configuration the domain does not interpret."""
