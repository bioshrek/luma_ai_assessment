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
| `epic_kitchens_mini` | D      | 3 videos x 2 segments, **layers deliberately uneven** |

Only C is edited rather than merely sliced: one real episode carries ~55 MB of inlined camera
frames, which no repository should hold. The step slice keeps the first 10 steps and the last
4, so `is_first` and the trailing `is_last` boundary block both survive.

D is sliced per layer, and the unevenness is the point: `P01_01` has neither camera poses nor
IMU, `P01_103` has IMU only, `P28_101` has both. Two episodes of one source must end up with
different `capabilities_json`, or M4 proves nothing.
"""

from __future__ import annotations

import csv
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
EPIC_SOURCE = SPIKE / "epic"

RLDS_EPISODES = 2
RLDS_HEAD_STEPS = 10
RLDS_TAIL_STEPS = 4
RLDS_EMPTIED = (
    "steps/observation/image",
    "steps/observation/hand_image",
    "steps/observation/image_with_depth",
)

EPIC_VIDEOS = ("P01_01", "P01_103", "P28_101")
# Named, not "the first two": `P28_101_43` is the one segment in the fixture that EPIC-Fields
# only partially reconstructed (32 of 48 frames), and it is the only real evidence we have that
# unregistered frames stay NaN and that POSE_COVERAGE can reach REVIEW.
EPIC_SEGMENTS: dict[str, tuple[str, ...]] = {
    "P01_01": ("P01_01_0", "P01_01_1"),
    "P01_103": ("P01_103_0", "P01_103_1"),
    "P28_101": ("P28_101_0", "P28_101_43"),
}
EPIC_POSE_VIDEOS = ("P28_101",)  # the only published example reconstruction
EPIC_IMU_VIDEOS = ("P01_103", "P28_101")  # extension-era videos carry GoPro metadata
# Seconds kept in the IMU fixture on top of the chosen segments' windows. No episode reads
# them: they are the committed evidence for ADR 012. In this range P28_101's gyroscope and
# accelerometer timestamps drift apart (793 of the video's 141,924 samples disagree, by up to
# 15 ms) — the measurement that says the two sensors are two clocks, not one.
EPIC_IMU_EVIDENCE: dict[str, tuple[tuple[float, float], ...]] = {"P28_101": ((409.0, 409.5),)}


# -- A and B: LeRobot v3.0 ------------------------------------------------------------------


def make_lerobot(source: Path, dest: Path, n_episodes: int, split_last: bool = False) -> None:
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
    _write_table(dest / "meta/tasks.parquet", pq.read_table(source / "meta/tasks.parquet"))
    if split_last:
        episodes, files = _split_last_data_file(episodes, data, n_episodes)
    else:
        files = {0: data}
    _write_table(dest / "meta/episodes/chunk-000/file-000.parquet", episodes)
    for file_index, table in files.items():
        _write_table(dest / f"data/chunk-000/file-{file_index:03d}.parquet", table)
    _report(dest, f"{n_episodes} episodes, {n_frames} rows")


def _split_last_data_file(
    episodes: pa.Table, data: pa.Table, n_episodes: int
) -> tuple[pa.Table, dict[int, pa.Table]]:
    """Move the last episode into its own data file, as real LeRobot datasets do.

    `dataset_from_index` counts rows of the *whole dataset*, not of the file the rows live in,
    so every episode outside `file-000` has an offset the adapter must not use as a slice
    position. Real `aloha_sim_insertion` puts episodes 15+ in `file-001`; this reproduces that
    shape in 141 KB so the bug cannot come back.
    """
    boundary = int(episodes.column("dataset_from_index")[n_episodes - 1].as_py())
    file_index = [
        0 if i < n_episodes - 1 else 1 for i in range(n_episodes)
    ]
    column = episodes.schema.field("data/file_index").type
    episodes = episodes.set_column(
        episodes.schema.get_field_index("data/file_index"),
        "data/file_index",
        pa.array(file_index, type=column),
    )
    return episodes, {0: data.slice(0, boundary), 1: data.slice(boundary)}


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


# -- D: EPIC-KITCHENS-100, three layers on three servers ------------------------------------


def make_epic(source: Path, dest: Path) -> None:
    for required in ("EPIC_100_video_info.csv", "EPIC_100_train.csv"):
        if not (source / required).exists():
            raise SystemExit(f"missing {source / required}; run spikes/probe_epic.py first")
    if dest.exists():
        shutil.rmtree(dest)

    videos = {row["video_id"]: row for row in _rows(source / "EPIC_100_video_info.csv")}
    wanted = {nid: video for video, ids in EPIC_SEGMENTS.items() for nid in ids}
    chosen: dict[str, list[dict[str, str]]] = {video: [] for video in EPIC_VIDEOS}
    for row in _rows(source / "EPIC_100_train.csv"):
        video = wanted.get(row["narration_id"])
        if video is not None:
            chosen[video].append(row)

    segments = [row for video in EPIC_VIDEOS for row in chosen[video]]
    _write_csv(
        dest / "annotations/EPIC_100_video_info.csv", [videos[v] for v in EPIC_VIDEOS]
    )
    _write_csv(dest / "annotations/EPIC_100_train.csv", segments)

    for video in EPIC_POSE_VIDEOS:
        _epic_pose(source / f"{video}.json", dest / f"camera_pose/{video}.json",
                   chosen[video], float(videos[video]["fps"]))
    for video in EPIC_IMU_VIDEOS:
        participant = video.split("_")[0]
        for kind in ("gyro", "accl"):
            _epic_imu(
                source / f"{video}-{kind}.csv",
                dest / f"imu/{participant}/meta_data/{video}-{kind}.csv",
                chosen[video],
                EPIC_IMU_EVIDENCE.get(video, ()),
            )
    _report(dest, f"{len(segments)} segments across {len(EPIC_VIDEOS)} videos")


def _epic_pose(
    source: Path, dest: Path, segments: Sequence[Mapping[str, str]], fps: float
) -> None:
    """Keep the poses of the chosen segments' frames only, and drop `points` entirely.

    The point cloud is ~80k 3-D points describing the whole kitchen; it says nothing about any
    one segment and is most of the 18 MB. `camera` is kept so the fixture stays a recognisable
    EPIC-Fields document.
    """
    document = json.loads(source.read_text())
    wanted: set[str] = set()
    for row in segments:
        first = int(_epic_seconds(row["start_timestamp"]) * fps)
        last = int(_epic_seconds(row["stop_timestamp"]) * fps)
        wanted |= {f"frame_{index + 1:010d}.jpg" for index in range(first, last + 1)}
    images = {k: v for k, v in document["images"].items() if k in wanted}
    _write(dest, json.dumps({"camera": document["camera"], "images": images}, indent=1))


def _epic_imu(
    source: Path,
    dest: Path,
    segments: Sequence[Mapping[str, str]],
    evidence: Sequence[tuple[float, float]] = (),
) -> None:
    windows = [
        (
            _epic_seconds(row["start_timestamp"]) * 1000.0,
            _epic_seconds(row["stop_timestamp"]) * 1000.0,
        )
        for row in segments
    ]
    windows += [(lo * 1000.0, hi * 1000.0) for lo, hi in evidence]
    kept = [
        row
        for row in _rows(source)
        if any(lo <= float(row["Milliseconds"]) <= hi for lo, hi in windows)
    ]
    _write_csv(dest, kept)


def _epic_seconds(timestamp: str) -> float:
    hours, minutes, seconds = timestamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_csv(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    if not rows:
        raise SystemExit(f"{path}: no rows selected; the video/segment choice is wrong")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


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
    make_lerobot(ALOHA_SOURCE, FIXTURES / "lerobot_aloha_mini", n_episodes=2, split_last=True)
    make_rlds(RLDS_SOURCE, FIXTURES / "rlds_berkeley_mini")
    make_epic(EPIC_SOURCE, FIXTURES / "epic_kitchens_mini")


if __name__ == "__main__":
    main()
