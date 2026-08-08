"""M0 spike: probe source C (OXE / RLDS, `berkeley_autolab_ur5`).

Decision point (pre-authorised by the implementation plan): **TensorFlow/TFDS does not
install cleanly here.** `tensorflow 2.19` pins `protobuf<6`, while the `tensorflow-metadata`
protos that `tensorflow_datasets` imports are generated with gencode 6.31 — the import
aborts with `google.protobuf.runtime_version.VersionError` before any data is touched.

So this probe takes the pre-authorised fallback: **parse the TFRecord shard directly**.
That needs no TensorFlow at all — a TFRecord is a length-prefixed record stream, and each
record is a `tf.train.Example` protobuf, whose wire format is decoded here in ~80 lines.
`features.json` from the same bucket supplies dtypes, shapes, and the semantics
descriptions.

Usage:
    uv run --group spike python spikes/probe_rlds.py [--max-bytes 150000000]
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import requests

DATASET = "berkeley_autolab_ur5"
VERSION = "0.1.0"
BASE_URL = f"https://storage.googleapis.com/gresearch/robotics/{DATASET}/{VERSION}"
DATA_DIR = Path(__file__).parent / "_data" / "rlds" / DATASET


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# --------------------------------------------------------------------------------------
# Minimal protobuf wire-format reader (enough for tf.train.Example)
# --------------------------------------------------------------------------------------

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
    """Yield (field_number, payload). Payload is bytes for length-delimited fields."""
    pos, end = 0, len(buf)
    while pos < end:
        key, pos = read_varint(buf, pos)
        field, wire = key >> 3, key & 0x7
        if wire == 0:
            value, pos = read_varint(buf, pos)
            yield field, value
        elif wire == 1:
            yield field, buf[pos:pos + 8]
            pos += 8
        elif wire == 2:
            length, pos = read_varint(buf, pos)
            yield field, buf[pos:pos + length]
            pos += length
        elif wire == 5:
            yield field, buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")


def decode_packed(buf: bytes, kind: str) -> list[Any]:
    if kind == "float":
        return list(struct.unpack(f"<{len(buf) // 4}f", buf))
    values, pos = [], 0
    while pos < len(buf):
        value, pos = read_varint(buf, pos)
        values.append(value)
    return values


def parse_example(record: bytes) -> dict[str, tuple[str, list[Any]]]:
    """tf.train.Example -> {feature_name: (kind, values)}, kind in bytes/float/int64."""
    out: dict[str, tuple[str, list[Any]]] = {}
    for field, payload in iter_fields(record):
        if field != 1:  # Example.features
            continue
        for entry_field, entry in iter_fields(payload):
            if entry_field != 1:  # Features.feature map entry
                continue
            name, feature = "", b""
            for map_field, map_value in iter_fields(entry):
                if map_field == 1:
                    name = map_value.decode()
                elif map_field == 2:
                    feature = map_value
            for kind_field, kind_payload in iter_fields(feature):
                if kind_field == 1:  # BytesList
                    out[name] = ("bytes", [v for f, v in iter_fields(kind_payload) if f == 1])
                elif kind_field == 2:  # FloatList
                    inner = b"".join(v for f, v in iter_fields(kind_payload) if f == 1)
                    out[name] = ("float", decode_packed(inner, "float"))
                elif kind_field == 3:  # Int64List
                    inner = b"".join(v for f, v in iter_fields(kind_payload) if f == 1)
                    out[name] = ("int64", decode_packed(inner, "int64"))
    return out


def iter_tfrecords(path: Path) -> Iterator[bytes]:
    """TFRecord: [uint64 length][uint32 crc][payload][uint32 crc], repeated. CRCs unchecked."""
    with path.open("rb") as handle:
        while header := handle.read(12):
            if len(header) < 12:
                return
            (length,) = struct.unpack("<Q", header[:8])
            payload = handle.read(length)
            if len(payload) < length:
                return  # byte-range truncated shard: stop at the first incomplete record
            handle.read(4)
            yield payload


# --------------------------------------------------------------------------------------
# Bucket access
# --------------------------------------------------------------------------------------

def http_get_json(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return json.loads(response.content)


def download(url: str, dest: Path, max_bytes: int | None) -> Path:
    if dest.exists():
        print(f"  cached: {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Range": f"bytes=0-{max_bytes - 1}"} if max_bytes else {}
    written = 0
    with requests.get(url, headers=headers, stream=True, timeout=1800) as response:
        response.raise_for_status()
        total = response.headers.get("Content-Range", "").split("/")[-1]
        with dest.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=8 << 20):
                handle.write(chunk)
                written += len(chunk)
    print(f"  downloaded {written / 1e6:.1f} MB of {total or '?'} bytes -> {dest.name}")
    return dest


def describe_features(node: dict[str, Any], path: str = "", depth: int = 0) -> None:
    indent = "  " * (depth + 1)
    if "featuresDict" in node:
        for name, child in node["featuresDict"]["features"].items():
            describe_features(child, f"{path}/{name}" if path else name, depth)
        return
    for wrapper in ("sequence", "dataset"):
        if wrapper in node:
            print(f"{indent}{path}: {wrapper.upper()} (RLDS steps)")
            describe_features(node[wrapper]["feature"], path, depth + 1)
            return
    tensor = node.get("tensor") or node.get("image") or node.get("text") or {}
    dtype = tensor.get("dtype", "?")
    shape = tensor.get("shape", {}).get("dimensions", [])
    description = (node.get("description") or "").strip().replace("\n", " ")
    print(f"{indent}{path:44s} dtype={dtype!s:10s} shape={shape}")
    if description:
        print(f"{indent}{'':44s} desc: {description}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-bytes", type=int, default=150_000_000)
    args = parser.parse_args()

    rule("environment / TFDS decision")
    print(f"  python = {sys.version.split()[0]}")
    try:
        import tensorflow_datasets as tfds

        # TFDS lazy-imports its protos, so a bare `import` is not a usable-ness test.
        tfds.features.FeatureConnector.from_json({"featuresDict": {"features": {}}})
        print("  tensorflow_datasets usable (fallback not required)")
    except Exception as exc:  # reporting the failure is the entire point of this check
        print(f"  tensorflow_datasets UNUSABLE: {type(exc).__name__}: {str(exc)[:200]}")
        print("  -> using the pre-authorised fallback: direct TFRecord parsing, no TensorFlow")

    rule(f"{DATASET}/{VERSION} — dataset_info.json")
    info = http_get_json(f"{BASE_URL}/dataset_info.json")
    for key in ("name", "version", "fileFormat", "description"):
        if key in info:
            print(f"  {key:12s} = {str(info[key])[:180]!r}")
    splits = info.get("splits", [])
    for split in splits:
        shard_lengths = [int(n) for n in split.get("shardLengths", [])]
        print(
            f"  split {split['name']!r}: episodes={sum(shard_lengths)} "
            f"shards={len(shard_lengths)} episodes_in_shard_0={shard_lengths[0]}"
        )
    print("  NOTE: identity is (split, shard, index-in-shard) only — no stable upstream id.")

    rule("features.json — the authoritative channel semantics")
    features_json = http_get_json(f"{BASE_URL}/features.json")
    describe_features(features_json)

    rule("first shard — decode episode 0 by direct TFRecord parsing")
    split = splits[0]
    num_shards = len(split["shardLengths"])
    shard_name = f"{DATASET}-{split['name']}.tfrecord-00000-of-{num_shards:05d}"
    shard_path = download(f"{BASE_URL}/{shard_name}", DATA_DIR / shard_name, args.max_bytes)

    record = next(iter_tfrecords(shard_path))
    example = parse_example(record)
    print(f"  episode 0 record size = {len(record) / 1e6:.2f} MB")
    print(f"  flattened feature keys ({len(example)}):")
    for name in sorted(example):
        kind, values = example[name]
        print(f"    {name:44s} kind={kind:6s} len={len(values)}")

    step_counts = {
        len(values)
        for name, (kind, values) in example.items()
        if name.startswith("steps/") and kind != "float"
    }
    print(f"\n  candidate step counts (non-float step features): {sorted(step_counts)}")
    n_steps = min(step_counts) if step_counts else 0

    print("\n  -- per-step numeric channels --")
    for name in sorted(example):
        kind, values = example[name]
        if (
            kind == "float"
            and n_steps
            and len(values) % n_steps == 0
            and len(values) // n_steps < 64
        ):
            width = len(values) // n_steps
            arr = np.asarray(values, dtype=np.float32).reshape(n_steps, width)
            print(f"    {name:44s} shape=({n_steps}, {width})")
            print(f"      step0={np.round(arr[0], 5).tolist()}")
            print(f"      step1={np.round(arr[1], 5).tolist()}")
            print(f"      last ={np.round(arr[-1], 5).tolist()}")
            print(
                f"      |v| p50={np.percentile(np.abs(arr), 50):.5f} "
                f"p99={np.percentile(np.abs(arr), 99):.5f} max={np.abs(arr).max():.5f}"
            )

    print("\n  -- boundary flags --")
    for name in ("steps/is_first", "steps/is_last", "steps/is_terminal"):
        if name in example:
            values = example[name][1]
            print(f"    {name:26s} first3={values[:3]} last3={values[-3:]}")
    for name in ("steps/language_instruction", "episode_metadata/file_path"):
        if name in example:
            values = example[name][1]
            print(f"    {name:26s} n={len(values)} first={values[0][:120]!r}")

    print("\n  -- no timestamp field anywhere: time must be synthesized --")
    print(f"    timestamp-like keys: {[k for k in example if 'time' in k.lower()]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
