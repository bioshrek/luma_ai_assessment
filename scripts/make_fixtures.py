"""Build the committed mini fixtures from the M0/M3 spike downloads.

Run once, by hand, when a fixture needs regenerating:

    uv run python scripts/make_fixtures.py

Each output is a valid but tiny copy of an upstream repository, so adapter characterization
tests run offline and in milliseconds. A wrong channel mapping is the most insidious bug in
this project; these fixtures are what pin it down.

| Fixture              | Source | Content                                             |
| -------------------- | ------ | --------------------------------------------------- |
| `lerobot_pusht_mini` | A      | 3 episodes, 420 rows, verbatim                      |
| `lerobot_aloha_mini` | B      | 2 episodes, 1000 rows, verbatim                     |
| `rlds_berkeley_mini` | C      | 2 episodes, 14 steps each, **camera bytes emptied** |

Only C is edited rather than merely sliced: one real episode carries ~55 MB of inlined camera
frames, which no repository should hold. The step slice keeps the first 10 steps and the last
4, so `is_first` and the trailing `is_last` boundary block both survive.
"""

from __future__ import annotations

import json
import shutil
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from rdp.infrastructure.sources.tfrecord import iter_records, parse_example

REPO = Path(__file__).resolve().parents[1]
SPIKE = REPO / "spikes/_data"
FIXTURES = REPO / "tests/fixtures"

PUSHT_SOURCE = SPIKE / "lerobot/lerobot__pusht"
ALOHA_SOURCE = SPIKE / "lerobot/lerobot__aloha_sim_insertion_human"
RLDS_SOURCE = SPIKE / "rlds/berkeley_autolab_ur5"
RLDS_SHARD = "berkeley_autolab_ur5-train.tfrecord-00000-of-00412"

RLDS_EPISODES = 2
RLDS_HEAD_STEPS = 10
RLDS_TAIL_STEPS = 4
RLDS_EMPTIED = (
    "steps/observation/image",
    "steps/observation/hand_image",
    "steps/observation/image_with_depth",
)


# -- A and B: LeRobot v3.0 ------------------------------------------------------------------


def make_lerobot(source: Path, dest: Path, n_episodes: int) -> None:
    if not source.exists():
        raise SystemExit(f"missing {source}; run the M0 spike download first")
    if dest.exists():
        shutil.rmtree(dest)

    episodes = pq.read_table(source / "meta/episodes/chunk-000/file-000.parquet").slice(
        0, n_episodes
    )
    # Keep only the columns the adapter actually reads: the per-episode `stats/*` block is
    # roughly 60 columns of upstream summary we never consult.
    keep = [name for name in episodes.column_names if not name.startswith("stats/")]
    episodes = episodes.select(keep)
    n_frames = int(episodes.column("dataset_to_index")[n_episodes - 1].as_py())

    data = pq.read_table(source / "data/chunk-000/file-000.parquet").slice(0, n_frames)

    info = json.loads((source / "meta/info.json").read_text())
    info["total_episodes"] = n_episodes
    info["total_frames"] = n_frames
    info["total_videos"] = 0

    _write(dest / "meta/info.json", json.dumps(info, indent=2))
    _write_table(dest / "meta/episodes/chunk-000/file-000.parquet", episodes)
    _write_table(dest / "meta/tasks.parquet", pq.read_table(source / "meta/tasks.parquet"))
    _write_table(dest / "data/chunk-000/file-000.parquet", data)
    _report(dest, f"{n_episodes} episodes, {n_frames} rows")


# -- C: RLDS TFRecord -----------------------------------------------------------------------


def make_rlds(source: Path, dest: Path) -> None:
    shard = source / RLDS_SHARD
    for required in (shard, source / "dataset_info.json", source / "features.json"):
        if not required.exists():
            raise SystemExit(f"missing {required}; run spikes/probe_rlds.py first")
    if dest.exists():
        shutil.rmtree(dest)

    records = []
    with shard.open("rb") as handle:
        for index, record in enumerate(iter_records(handle)):
            if index >= RLDS_EPISODES:
                break
            example = {n: (f.kind, f.values) for n, f in parse_example(record).items()}
            records.append(_encode_example(_shrink(example)))

    info = json.loads((source / "dataset_info.json").read_text())
    info["splits"] = [{"name": "train", "shardLengths": [str(len(records))]}]
    _write(dest / "dataset_info.json", json.dumps(info, indent=2))
    _write(dest / "features.json", (source / "features.json").read_text())

    name = "berkeley_autolab_ur5-train.tfrecord-00000-of-00001"  # one shard holding both
    _write_bytes(dest / name, b"".join(_frame_tfrecord(record) for record in records))
    _report(dest, f"{len(records)} episodes, {RLDS_HEAD_STEPS + RLDS_TAIL_STEPS} steps each")


def _shrink(example: Mapping[str, tuple[str, list[Any]]]) -> dict[str, tuple[str, list[Any]]]:
    n_steps = len(example["steps/is_last"][1])
    keep = list(range(RLDS_HEAD_STEPS)) + list(range(n_steps - RLDS_TAIL_STEPS, n_steps))
    out: dict[str, tuple[str, list[Any]]] = {}
    for name, (kind, values) in example.items():
        if name in RLDS_EMPTIED:
            # The pixels are what makes a real record 55 MB. The feature key, its length and
            # its position all survive; only the payloads are emptied, which is exactly what
            # `CameraSpec.is_present` is supposed to notice.
            out[name] = (kind, [b"" for _ in keep])
            continue
        width, remainder = divmod(len(values), n_steps)
        if remainder:
            raise SystemExit(f"{name}: {len(values)} values do not divide {n_steps} steps")
        out[name] = (kind, [values[i * width + j] for i in keep for j in range(width)])
    return out


# -- a minimal TFRecord / `tf.train.Example` writer -----------------------------------------
#
# The reader is production code (`infrastructure/sources/tfrecord.py`); only the fixture
# builder ever needs to write, so the encoder stays here rather than widening that module.


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _delimited(field: int, payload: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(payload)) + payload


def _encode_feature(kind: str, values: Sequence[Any]) -> bytes:
    if kind == "bytes":
        return _delimited(1, b"".join(_delimited(1, value) for value in values))
    if kind == "float":
        return _delimited(2, _delimited(1, struct.pack(f"<{len(values)}f", *values)))
    return _delimited(3, _delimited(1, b"".join(_varint(int(v)) for v in values)))


def _encode_example(features: Mapping[str, tuple[str, list[Any]]]) -> bytes:
    entries = b"".join(
        _delimited(
            1, _delimited(1, name.encode()) + _delimited(2, _encode_feature(kind, values))
        )
        for name, (kind, values) in sorted(features.items())
    )
    return _delimited(1, entries)


def _crc32c_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
        table.append(crc)
    return table


_CRC32C = _crc32c_table()


def _masked_crc(data: bytes) -> int:
    """The CRC our reader deliberately skips — written correctly anyway, so the fixture is a
    genuine TFRecord that any other tool can also open."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC32C[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    crc ^= 0xFFFFFFFF
    return (((crc >> 15) | (crc << 17)) + 0xA282EAD8) & 0xFFFFFFFF


def _frame_tfrecord(payload: bytes) -> bytes:
    header = struct.pack("<Q", len(payload))
    return (
        header
        + struct.pack("<I", _masked_crc(header))
        + payload
        + struct.pack("<I", _masked_crc(payload))
    )


# -- shared ---------------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    _write_bytes(path, text.encode())


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_table(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def _report(dest: Path, what: str) -> None:
    total = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    print(f"wrote {dest.relative_to(REPO)}: {what}, {total} bytes")


def main() -> None:
    make_lerobot(PUSHT_SOURCE, FIXTURES / "lerobot_pusht_mini", n_episodes=3)
    make_lerobot(ALOHA_SOURCE, FIXTURES / "lerobot_aloha_mini", n_episodes=2)
    make_rlds(RLDS_SOURCE, FIXTURES / "rlds_berkeley_mini")


if __name__ == "__main__":
    main()
