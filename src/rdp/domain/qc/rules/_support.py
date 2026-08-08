"""Shared numeric helpers for QC rules. Pure functions over already-selected channels.

Kept separate so that "how motion is measured" has exactly one definition: `STATIC_EPISODE`
and `ACTION_JERK` must not disagree about what a delta channel's step size is.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
from numpy.typing import NDArray

from rdp.domain.action_spec import Channel, SignalOrigin
from rdp.domain.qc.rule import QCEpisodeView


def derived_basis(meta: QCEpisodeView, signals: Iterable[str]) -> str | None:
    """Invariant 13, for a rule the engine cannot help: name the origins, or None if measured.

    The engine downgrades a FAIL using `rule.required_levels`, which an **ungated** rule such as
    `STATIC_EPISODE` deliberately leaves empty — it must run on episodes that have no per-frame
    signal at all. Such a rule has to ask the question itself about the signals it actually read.
    """
    origins: set[SignalOrigin] = set()
    for signal in signals:
        origins |= meta.origins_of(signal)
    if not origins or SignalOrigin.MEASURED in origins:
        return None
    return ",".join(sorted(origin.value for origin in origins))


def step_magnitudes(values: NDArray[np.float64], channel: Channel) -> NDArray[np.float64]:
    """Per-step motion commanded or observed on one channel, always non-negative.

    An **absolute** channel moves by the difference between consecutive samples. A **delta**
    channel already *is* that difference — differencing it again would report the second
    derivative and call a constant velocity "motionless" (ADR 003: C's whole action vector is
    a delta, and 0 means "stay put", not "no data").

    Both forms return `n_frames - 1` values so that channels of either kind can be compared
    step-for-step within one episode; a delta channel therefore contributes every command but
    its first.
    """
    array = np.asarray(values, dtype=np.float64)
    if array.size < 2:
        return np.empty(0, dtype=np.float64)
    steps: NDArray[np.float64] = array[1:] if channel.is_delta else np.diff(array)
    return np.abs(steps)
