"""`FrameTable` — the per-frame numeric payload and the `frames.parquet` column contract.

Columns are `t`, `action.<channel>`, `state.<channel>`, `raw.<upstream field>` (design §2.4).
Consumers select **by name**; positional indexing is not part of the contract.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from rdp.domain.action_spec import SignalSpec
from rdp.domain.errors import InvariantViolation

TIME_COLUMN = "t"
ACTION_PREFIX = "action."
STATE_PREFIX = "state."
RAW_PREFIX = "raw."


@dataclass(frozen=True, eq=False)
class FrameTable:
    columns: Mapping[str, NDArray[Any]]
    raw_frame_columns: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if TIME_COLUMN not in self.columns:
            raise InvariantViolation("frames must have a 't' column of seconds from episode start")
        lengths = {name: array.shape[0] for name, array in self.columns.items()}
        if len(set(lengths.values())) > 1:
            raise InvariantViolation(f"ragged frame table: column lengths {lengths}")
        for name, array in self.columns.items():
            if array.ndim != 1:
                raise InvariantViolation(f"column {name!r} must be 1-D, got shape {array.shape}")
        if self.columns[TIME_COLUMN].dtype != np.float64:
            raise InvariantViolation("column 't' must be float64 seconds")
        declared = set(self.raw_frame_columns)
        for name in self.columns:
            if name == TIME_COLUMN:
                continue
            if name.startswith(RAW_PREFIX):
                # Invariant 12: unmodeled columns are prefixed *and* registered.
                if name not in declared:
                    raise InvariantViolation(
                        f"column {name!r} is not registered in raw_frame_columns (invariant 12)"
                    )
                continue
            if not name.startswith((ACTION_PREFIX, STATE_PREFIX)):
                raise InvariantViolation(
                    f"column {name!r} violates the column-name contract "
                    f"(expected 't', 'action.*', 'state.*' or 'raw.*'; invariant 12)"
                )
        missing = declared - set(self.columns)
        if missing:
            raise InvariantViolation(f"raw_frame_columns lists absent columns {sorted(missing)}")

    @property
    def n_frames(self) -> int:
        return int(self.columns[TIME_COLUMN].shape[0])

    @property
    def t(self) -> NDArray[Any]:
        return self.columns[TIME_COLUMN]

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(self.columns)

    def column(self, name: str) -> NDArray[Any]:
        try:
            return self.columns[name]
        except KeyError as exc:
            raise InvariantViolation(f"no column {name!r} in frames") from exc

    def has_column(self, name: str) -> bool:
        return name in self.columns

    def physical_view(self, spec: SignalSpec) -> dict[str, NDArray[Any]]:
        """Invariant 6: cross-channel statistics only ever see physical channels.

        Rules receive this, never the full vector, so a control flag such as C's
        `terminate_episode` cannot leak into a range or jerk computation.
        """
        prefix = spec.column_prefix
        return {c.name: self.column(f"{prefix}.{c.name}") for c in spec.physical_channels}

    def canonical_digest(self, column_order: Iterable[str]) -> str:
        """sha256 over **logical content**, not container bytes (design §5).

        Parquet file bytes depend on the compressor, row-group layout and writer version, so
        identical content would hash differently. Here: a key-sorted metadata header plus each
        column's values as float64 little-endian.
        """
        order = [name for name in column_order if name in self.columns]
        order += sorted(set(self.columns) - set(order))
        header = {
            "columns": order,
            "dtypes": [str(self.columns[name].dtype) for name in order],
            "n_rows": self.n_frames,
        }
        digest = hashlib.sha256()
        digest.update(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
        for name in order:
            values = np.ascontiguousarray(self.columns[name].astype("<f8"))
            digest.update(values.tobytes())
        return digest.hexdigest()
