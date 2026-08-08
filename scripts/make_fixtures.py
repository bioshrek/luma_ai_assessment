"""Build the committed mini fixtures from the M0 spike downloads.

Run once, by hand, when the fixture needs regenerating:

    uv run python scripts/make_fixtures.py

The output is a valid but tiny LeRobot v3.0 repository (3 episodes, 420 rows), so adapter
characterization tests run offline and in milliseconds. A wrong channel mapping is the most
insidious bug in this project; these fixtures are what pin it down.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "spikes/_data/lerobot/lerobot__pusht"
DEST = REPO / "tests/fixtures/lerobot_pusht_mini"
N_EPISODES = 3


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"missing {SOURCE}; run the M0 spike download first")
    if DEST.exists():
        shutil.rmtree(DEST)

    episodes = pq.read_table(SOURCE / "meta/episodes/chunk-000/file-000.parquet").slice(
        0, N_EPISODES
    )
    # Keep only the columns the adapter actually reads: the per-episode `stats/*` block is
    # roughly 60 columns of upstream summary we never consult.
    keep = [name for name in episodes.column_names if not name.startswith("stats/")]
    episodes = episodes.select(keep)
    n_frames = int(episodes.column("dataset_to_index")[N_EPISODES - 1].as_py())

    data = pq.read_table(SOURCE / "data/chunk-000/file-000.parquet").slice(0, n_frames)

    info = json.loads((SOURCE / "meta/info.json").read_text())
    info["total_episodes"] = N_EPISODES
    info["total_frames"] = n_frames
    info["total_videos"] = 0

    _write(DEST / "meta/info.json", json.dumps(info, indent=2))
    _write_table(DEST / "meta/episodes/chunk-000/file-000.parquet", episodes)
    _write_table(DEST / "meta/tasks.parquet", pq.read_table(SOURCE / "meta/tasks.parquet"))
    _write_table(DEST / "data/chunk-000/file-000.parquet", data)

    total = sum(p.stat().st_size for p in DEST.rglob("*") if p.is_file())
    print(f"wrote {DEST.relative_to(REPO)}: {N_EPISODES} episodes, {n_frames} rows, {total} bytes")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _write_table(path: Path, table: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
