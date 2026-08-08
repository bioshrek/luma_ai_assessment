"""Shared fixtures: a self-contained workspace wired to the committed mini fixture."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from rdp.interfaces.wiring import Container

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests/fixtures/lerobot_pusht_mini"

_SOURCES = """
version: 1
defaults:
  with_video: false
sources:
  - source_id: pusht
    kind: lerobot
    uri: {uri}
    revision: main
    embodiment: pusht_planar
    license: apache-2.0
    max_episodes: 3
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Container]:
    """A `Container` on a throwaway store, reading the real config but the mini dataset.

    The URI is a plain local path, so the whole integration suite runs offline.
    """
    config = tmp_path / "config"
    config.mkdir()
    (config / "sources.yaml").write_text(_SOURCES.format(uri=FIXTURE))
    for name in ("embodiments.yaml", "qc.yaml"):
        shutil.copy(REPO / "config" / name, config / name)

    container = Container(
        store=tmp_path / "store", config=config, reports=tmp_path / "reports"
    )
    try:
        yield container
    finally:
        container.catalog.close()
