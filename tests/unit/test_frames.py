"""`FrameTable`: the `frames.parquet` column contract (invariant 12) and the content digest."""

from __future__ import annotations

import numpy as np
import pytest
from tests.factories import frames, spec

from rdp.domain.errors import InvariantViolation
from rdp.domain.frames import FrameTable


def test_time_column_is_mandatory_and_float64() -> None:
    with pytest.raises(InvariantViolation, match="'t' column"):
        FrameTable(columns={"action.ee.x": np.zeros(3)})
    with pytest.raises(InvariantViolation, match="float64"):
        FrameTable(columns={"t": np.zeros(3, dtype=np.float32)})


def test_columns_must_be_equal_length_and_one_dimensional() -> None:
    with pytest.raises(InvariantViolation, match="ragged"):
        FrameTable(columns={"t": np.zeros(3), "action.ee.x": np.zeros(2)})
    with pytest.raises(InvariantViolation, match="1-D"):
        FrameTable(columns={"t": np.zeros(3), "action.ee.x": np.zeros((3, 2))})


def test_unprefixed_columns_are_rejected() -> None:
    with pytest.raises(InvariantViolation, match="column-name contract"):
        FrameTable(columns={"t": np.zeros(3), "reward": np.zeros(3)})


def test_raw_columns_must_be_registered_in_both_directions() -> None:
    columns = {"t": np.zeros(3), "raw.next.reward": np.zeros(3)}
    with pytest.raises(InvariantViolation, match="invariant 12"):
        FrameTable(columns=columns)
    with pytest.raises(InvariantViolation, match="absent columns"):
        FrameTable(columns={"t": np.zeros(3)}, raw_frame_columns=("raw.next.reward",))
    table = FrameTable(columns=columns, raw_frame_columns=("raw.next.reward",))
    assert table.has_column("raw.next.reward")


def test_physical_view_hides_non_physical_channels() -> None:
    view = frames().physical_view(spec(is_command=True))
    assert sorted(view) == ["ee.x", "ee.y"]


def test_canonical_digest_is_stable_across_column_insertion_order() -> None:
    a = frames()
    reordered = FrameTable(columns=dict(reversed(list(a.columns.items()))))
    order = ["t", *spec(True).column_names(), *spec(False).column_names()]
    assert a.canonical_digest(order) == reordered.canonical_digest(order)


def test_canonical_digest_changes_when_a_value_changes() -> None:
    a = frames()
    changed = dict(a.columns)
    changed["action.ee.x"] = changed["action.ee.x"] + 1.0
    order = ["t", *spec(True).column_names(), *spec(False).column_names()]
    assert a.canonical_digest(order) != FrameTable(columns=changed).canonical_digest(order)
