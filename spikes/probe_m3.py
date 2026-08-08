"""M3 probe: measure what sources B and C actually contain, before writing their adapters.

Every number the M3 adapters and ADRs assert comes from here, not from documentation. Reads
only the M0 spike downloads under `spikes/_data/`; no network.

Usage:
    uv run python spikes/probe_m3.py > spikes/_out/probe_m3.txt
"""

from __future__ import annotations

import json
import struct
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
ALOHA = REPO / "spikes/_data/lerobot/lerobot__aloha_sim_insertion_human"
SHARD = (
    REPO
    / "spikes/_data/rlds/berkeley_autolab_ur5/berkeley_autolab_ur5-train.tfrecord-00000-of-00412"
)


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# -- B: aloha -----------------------------------------------------------------------------


def probe_aloha() -> None:
    rule("B — lerobot/aloha_sim_insertion_human")
    info = json.loads((ALOHA / "meta/info.json").read_text())
    names = info["features"]["action"]["names"]["motors"]
    table = pq.read_table(ALOHA / "data/chunk-000/file-000.parquet")
    action = np.asarray(table.column("action").to_pylist(), dtype=np.float64)
    state = np.asarray(table.column("observation.state").to_pylist(), dtype=np.float64)
    print(f"  rows={table.num_rows} fps={info['fps']} action={action.shape} state={state.shape}")
    print(f"  {'channel':22s} {'action min':>11s} {'action max':>11s} "
          f"{'state min':>11s} {'state max':>11s}")
    for i, name in enumerate(names):
        print(
            f"  {name:22s} {action[:, i].min():11.5f} {action[:, i].max():11.5f} "
            f"{state[:, i].min():11.5f} {state[:, i].max():11.5f}"
        )

    rule("B — is the gripper channel monotone-open or an absolute normalized opening?")
    for i, name in enumerate(names):
        if "gripper" not in name:
            continue
        column = action[:, i]
        print(
            f"  action {name}: p0={column.min():.5f} p50={np.median(column):.5f} "
            f"p100={column.max():.5f} n_unique={len(np.unique(np.round(column, 6)))}"
        )
        print(f"    first 10 = {np.round(column[:10], 5).tolist()}")

    rule("B — is `timestamp` a clock, or frame_index/fps restated? (the ADR 005 test)")
    timestamp = np.asarray(table.column("timestamp").to_numpy(zero_copy_only=False))
    frame_index = np.asarray(table.column("frame_index").to_numpy(zero_copy_only=False))
    synthetic = (frame_index / float(info["fps"])).astype(np.float32)
    print(f"  bit-identical to float32(frame_index/fps): "
          f"{np.array_equal(synthetic, timestamp.astype(np.float32))}")

    rule("B — boundary columns and camera features")
    print(f"  columns = {table.column_names}")
    for key, feature in info["features"].items():
        if feature.get("dtype") in ("video", "image"):
            print(f"  camera {key}: dtype={feature['dtype']} shape={feature['shape']}")


# -- C: RLDS ------------------------------------------------------------------------------


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def iter_fields(buf: bytes) -> Iterator[tuple[int, Any]]:
    pos, end = 0, len(buf)
    while pos < end:
        key, pos = read_varint(buf, pos)
        field, wire = key >> 3, key & 0x7
        if wire == 0:
            value, pos = read_varint(buf, pos)
            yield field, value
        elif wire == 2:
            length, pos = read_varint(buf, pos)
            yield field, buf[pos : pos + length]
            pos += length
        elif wire == 1:
            yield field, buf[pos : pos + 8]
            pos += 8
        elif wire == 5:
            yield field, buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")


def parse_example(record: bytes) -> dict[str, tuple[str, list[Any]]]:
    out: dict[str, tuple[str, list[Any]]] = {}
    for field, payload in iter_fields(record):
        if field != 1:
            continue
        for entry_field, entry in iter_fields(payload):
            if entry_field != 1:
                continue
            name, feature = "", b""
            for map_field, map_value in iter_fields(entry):
                if map_field == 1:
                    name = map_value.decode()
                elif map_field == 2:
                    feature = map_value
            for kind_field, kind_payload in iter_fields(feature):
                if kind_field == 1:
                    out[name] = ("bytes", [v for f, v in iter_fields(kind_payload) if f == 1])
                elif kind_field == 2:
                    inner = b"".join(v for f, v in iter_fields(kind_payload) if f == 1)
                    out[name] = ("float", list(struct.unpack(f"<{len(inner) // 4}f", inner)))
                elif kind_field == 3:
                    inner = b"".join(v for f, v in iter_fields(kind_payload) if f == 1)
                    values, pos = [], 0
                    while pos < len(inner):
                        value, pos = read_varint(inner, pos)
                        values.append(value)
                    out[name] = ("int64", values)
    return out


def iter_tfrecords(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while header := handle.read(12):
            if len(header) < 12:
                return
            (length,) = struct.unpack("<Q", header[:8])
            payload = handle.read(length)
            if len(payload) < length:
                return
            handle.read(4)
            yield payload


def probe_rlds() -> None:
    rule("C — berkeley_autolab_ur5: every episode in the cached shard prefix")
    for index, record in enumerate(iter_tfrecords(SHARD)):
        example = parse_example(record)
        is_last = example["steps/is_last"][1]
        is_terminal = example["steps/is_terminal"][1]
        is_first = example["steps/is_first"][1]
        n = len(is_last)
        terminate = np.asarray(example["steps/action/terminate_episode"][1])
        gripper = np.asarray(example["steps/action/gripper_closedness_action"][1])
        world = np.asarray(example["steps/action/world_vector"][1]).reshape(n, 3)
        reward = np.asarray(example["steps/reward"][1])
        instruction = example["steps/observation/natural_language_instruction"][1]
        print(f"\n  -- episode {index}: {n} steps, record {len(record) / 1e6:.1f} MB")
        print(f"     is_first  sum={sum(is_first)} at={np.flatnonzero(is_first).tolist()}")
        print(f"     is_last   sum={sum(is_last)} at={np.flatnonzero(is_last).tolist()}")
        print(f"     is_terminal sum={sum(is_terminal)} at={np.flatnonzero(is_terminal).tolist()}")
        print(f"     terminate_episode nonzero at={np.flatnonzero(terminate).tolist()}")
        print(f"     gripper unique={np.unique(gripper).tolist()[:10]}")
        print(f"     world_vector zero rows at={np.flatnonzero(~world.any(axis=1)).tolist()}")
        print(f"     reward max={reward.max():.3f} nonzero at={np.flatnonzero(reward).tolist()}")
        print(f"     instruction unique={ {v for v in instruction} }")
        print(f"     bytes per feature: "
              f"{ {k: sum(len(b) for b in v[1]) for k, v in example.items() if v[0] == 'bytes'} }")
        if index >= 2:
            break

    rule("C — feature byte budget for a mini fixture")
    record = next(iter_tfrecords(SHARD))
    example = parse_example(record)
    for name in sorted(example):
        kind, values = example[name]
        if kind == "bytes":
            size = sum(len(v) for v in values)
        elif kind == "float":
            size = 4 * len(values)
        else:
            size = len(values)
        print(f"    {name:48s} kind={kind:6s} approx_bytes={size}")


def main() -> int:
    probe_aloha()
    probe_rlds()
    return 0


if __name__ == "__main__":
    sys.exit(main())
