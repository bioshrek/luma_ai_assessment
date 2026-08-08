"""Per-channel statistics. Metadata only — raw values are never rewritten (design §2.2b)."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict

from rdp.domain.action_spec import SignalSpec
from rdp.domain.frames import FrameTable


class ChannelStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int
    n_nan: int
    mean: float | None
    std: float | None
    min: float | None
    max: float | None
    p1: float | None
    p99: float | None


def _stats(values: NDArray[np.float64]) -> ChannelStats:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return ChannelStats(
            count=int(values.size),
            n_nan=int(values.size),
            mean=None,
            std=None,
            min=None,
            max=None,
            p1=None,
            p99=None,
        )
    return ChannelStats(
        count=int(values.size),
        n_nan=int(values.size - finite.size),
        mean=float(finite.mean()),
        std=float(finite.std()),
        min=float(finite.min()),
        max=float(finite.max()),
        p1=float(np.percentile(finite, 1)),
        p99=float(np.percentile(finite, 99)),
    )


def summarize(values: Sequence[float]) -> ChannelStats:
    """The same summary, over an arbitrary sample — the shape `rdp stats` reports thresholds in.

    Lives here rather than in a second statistics module so that "distribution" means one thing
    in this codebase: a QC metric across episodes is summarized exactly as a channel is.
    """
    return _stats(np.asarray(values, dtype=np.float64))


def channel_stats(frames: FrameTable, *specs: SignalSpec) -> dict[str, ChannelStats]:
    """Statistics over **physical** channels only, keyed by full column name."""
    out: dict[str, ChannelStats] = {}
    for spec in specs:
        if not spec.is_per_frame:
            continue
        for name, values in frames.physical_view(spec).items():
            out[f"{spec.column_prefix}.{name}"] = _stats(values.astype(np.float64))
    return out
