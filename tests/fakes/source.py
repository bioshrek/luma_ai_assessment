"""`FakeSource` — a `SourcePort` with no upstream, and a call counter that survives a crash.

The counter is the instrument the resume assertions are built on: "it did not crash again" is
not an assertion, but "`fetch` was called exactly as many times across two runs as in one
uninterrupted run" is.

It lives in a file rather than on the instance so the same assertion works whether the crash was
an exception in-process or a real `os._exit` in a subprocess.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import numpy as np
from tests import factories

from rdp.application.ports import EpisodeRef, RawEpisode
from rdp.domain.episode import CanonicalEpisode
from rdp.domain.frames import FrameTable
from rdp.domain.source import Source

LIST = "list_episodes"
FETCH = "fetch"
NORMALIZE = "normalize"

_STAGED = "raw.json"


class FakeSource:
    def __init__(
        self,
        counter_path: Path,
        n_episodes: int = 3,
        n_frames: int = 5,
        adapter_version: str = "fake@1",
        salt: float = 0.0,
    ) -> None:
        self.counter_path = counter_path
        self.n_episodes = n_episodes
        self.n_frames = n_frames
        self._adapter_version = adapter_version
        self.salt = salt

    @property
    def kind(self) -> str:
        return "fake"

    @property
    def adapter_version(self) -> str:
        return self._adapter_version

    @adapter_version.setter
    def adapter_version(self, value: str) -> None:
        self._adapter_version = value

    # -- SourcePort ---------------------------------------------------------------------

    def list_episodes(self, source: Source) -> Iterator[EpisodeRef]:
        self._bump(LIST)
        for index in range(self.n_episodes):
            yield EpisodeRef(source_id=source.source_id, upstream_id=f"episode_{index:06d}")

    def fetch(self, ref: EpisodeRef, source: Source, dest: Path) -> RawEpisode:
        self._bump(FETCH)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / _STAGED).write_text(json.dumps({"salt": self.salt}))
        return RawEpisode(ref=ref, path=dest, upstream_revision=source.revision)

    def normalize(self, raw: RawEpisode, source: Source) -> CanonicalEpisode:
        self._bump(NORMALIZE)
        salt = json.loads((raw.path / _STAGED).read_text())["salt"]
        index = float(int(raw.ref.upstream_id.rsplit("_", 1)[1]))
        meta = factories.meta(
            n_frames=self.n_frames,
            upstream_id=raw.ref.upstream_id,
            source_id=raw.ref.source_id,
        )
        values = np.arange(self.n_frames, dtype=np.float64) + index * 100.0 + salt
        frames = FrameTable(
            columns={
                "t": np.arange(self.n_frames, dtype=np.float64) * 0.1,
                "action.ee.x": values,
                "action.ee.y": values + 1.0,
                "state.ee.x": values,
                "state.ee.y": values + 1.0,
            }
        )
        return CanonicalEpisode(meta=meta, frames=frames)

    # -- counters -----------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        if not self.counter_path.exists():
            return {}
        counts: dict[str, int] = json.loads(self.counter_path.read_text())
        return counts

    def _bump(self, name: str) -> None:
        counts = self.counts()
        counts[name] = counts.get(name, 0) + 1
        self.counter_path.parent.mkdir(parents=True, exist_ok=True)
        self.counter_path.write_text(json.dumps(counts, sort_keys=True))
