"""Is pusht's `timestamp` column a clock, or arithmetic? Evidence for ADR 005.

    uv run --no-group spike python spikes/probe_pusht_timestamps.py \\
        > spikes/_out/probe_pusht_timestamps.txt

Reads the M0 download in `spikes/_data/` (gitignored); the captured output is committed.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1] / "spikes/_data/lerobot/lerobot__pusht"


def main() -> None:
    info = json.loads((ROOT / "meta/info.json").read_text())
    fps = float(info["fps"])
    table = pq.read_table(ROOT / "data/chunk-000/file-000.parquet")
    timestamp = table.column("timestamp").to_numpy()
    frame_index = table.column("frame_index").to_numpy()
    episode_index = table.column("episode_index").to_numpy()

    # The reconstruction has to be done in float32: that is the column's dtype, and n/fps is
    # not representable in binary, so comparing in float64 would manufacture a difference.
    reconstructed = (frame_index / fps).astype(np.float32)

    print(f"rows {len(timestamp)}")
    print(f"fps from meta/info.json: {fps}")
    exact = np.array_equal(timestamp, reconstructed)
    print(f"timestamp == float32(frame_index/fps) exactly: {exact}")
    print(f"max abs diff: {np.max(np.abs(timestamp - reconstructed))}")

    first_of_episode_1 = timestamp[episode_index == 1][0]
    print(f"timestamp restarts per episode: ep1 first ts = {first_of_episode_1}")

    ep0 = timestamp[episode_index == 0]
    print(f"ep0 dt unique: {np.unique(np.diff(ep0))}")
    print(f"ep0 n frames: {len(ep0)}")

    lengths = np.bincount(episode_index)
    print(f"episodes: {len(lengths)}")
    print(f"len min/max/mean: {lengths.min()} {lengths.max()} {lengths.mean()}")


if __name__ == "__main__":
    main()
