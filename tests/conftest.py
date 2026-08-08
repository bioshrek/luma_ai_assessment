"""Shared fixtures: a self-contained workspace wired to the committed mini fixtures."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from rdp.interfaces.wiring import Container

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests/fixtures/lerobot_pusht_mini"
ALOHA_FIXTURE = REPO / "tests/fixtures/lerobot_aloha_mini"
RLDS_FIXTURE = REPO / "tests/fixtures/rlds_berkeley_mini"

_HEADER = """
version: 1
defaults:
  with_video: false
sources:
"""

_PUSHT = """  - source_id: pusht
    kind: lerobot
    uri: {pusht}
    revision: main
    embodiment: pusht_planar
    license: apache-2.0
    max_episodes: 3
"""

_ALOHA = """  - source_id: aloha_sim_insertion
    kind: lerobot
    uri: {aloha}
    revision: main
    embodiment: aloha_bimanual
    license: mit
    max_episodes: 2
"""

_RLDS = """  - source_id: berkeley_ur5
    kind: rlds
    uri: {rlds}
    revision: "0.1.0"
    split: train
    shard_layout_revision: "{layout}"
    embodiment: ur5_single_arm
    license: cc-by-4.0
    max_episodes: 2
    control_hz: 5
    drop_channels:
      - steps/observation/natural_language_embedding
"""

_SOURCES = _HEADER + _PUSHT

WorkspaceFactory = Callable[..., Container]


@pytest.fixture
def make_workspace(tmp_path: Path) -> Iterator[WorkspaceFactory]:
    """Build a `Container` over any subset of the mini fixtures, on a throwaway store.

    Every URI is a plain local path, so the whole integration suite runs offline. Rebuilding a
    container over the *same* store is how the staleness tests re-run a changed config.
    """
    containers: list[Container] = []

    def build(
        *, blocks: tuple[str, ...] = (_PUSHT,), store: str = "store", layout: str = ""
    ) -> Container:
        config = tmp_path / f"config-{len(containers)}"
        config.mkdir()
        text = _HEADER + "".join(blocks)
        (config / "sources.yaml").write_text(
            text.format(
                pusht=FIXTURE,
                aloha=ALOHA_FIXTURE,
                rlds=RLDS_FIXTURE,
                layout=layout or "train:1-shards@0.1.0",
            )
        )
        for name in ("embodiments.yaml", "qc.yaml"):
            shutil.copy(REPO / "config" / name, config / name)
        container = Container(
            store=tmp_path / store, config=config, reports=tmp_path / "reports"
        )
        containers.append(container)
        return container

    try:
        yield build
    finally:
        for container in containers:
            container.catalog.close()


@pytest.fixture
def workspace(make_workspace: WorkspaceFactory) -> Container:
    """A `Container` reading the real config but only the pusht mini dataset."""
    return make_workspace()

