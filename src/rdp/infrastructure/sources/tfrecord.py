"""A TFRecord + `tf.train.Example` reader in stdlib Python — no TensorFlow.

[ADR 001](../../../../docs/adr/001-rlds-reader-no-tensorflow.md): `tensorflow-datasets` cannot
be imported on the pinned interpreter (protobuf gencode 6.31 against runtime 5.29), so source C
is read directly. Two formats are all that is needed:

- **TFRecord framing**: `[uint64 length][uint32 crc][payload][uint32 crc]`, repeated.
- **`tf.train.Example`**: a protobuf whose only shapes we care about are the `Features` map and
  the three list kinds (`BytesList` / `FloatList` / `Int64List`).

CRC32C is not verified: a corrupt shard surfaces as a parse error instead, and `content_hash`
is computed over normalized bytes anyway (ADR 001, consequences).
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Any

BYTES = "bytes"
FLOAT = "float"
INT64 = "int64"

_HEADER = 12
_FOOTER = 4


@dataclass(frozen=True)
class Feature:
    kind: str
    values: list[Any]

    def __len__(self) -> int:
        return len(self.values)


class TFRecordError(Exception):
    """The stream is not a well-formed TFRecord / `tf.train.Example`."""


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _iter_fields(buf: bytes) -> Iterator[tuple[int, Any]]:
    """Yield `(field_number, payload)`; length-delimited payloads come back as bytes."""
    pos, end = 0, len(buf)
    while pos < end:
        key, pos = _read_varint(buf, pos)
        number, wire = key >> 3, key & 0x7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            yield number, value
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            yield number, buf[pos : pos + length]
            pos += length
        elif wire == 1:
            yield number, buf[pos : pos + 8]
            pos += 8
        elif wire == 5:
            yield number, buf[pos : pos + 4]
            pos += 4
        else:
            raise TFRecordError(f"unsupported protobuf wire type {wire}")


def iter_records(stream: IO[bytes]) -> Iterator[bytes]:
    """Yield record payloads, reading lazily so a caller can stop at the record it wants.

    A truncated tail ends the iteration rather than raising: byte-range reads of a shard are a
    supported way to avoid downloading gigabytes for the first few episodes.
    """
    while header := stream.read(_HEADER):
        if len(header) < _HEADER:
            return
        (length,) = struct.unpack("<Q", header[:8])
        payload = stream.read(length)
        if len(payload) < length:
            return
        stream.read(_FOOTER)
        yield payload


def parse_example(record: bytes) -> dict[str, Feature]:
    """`tf.train.Example` -> `{feature_name: Feature}`."""
    out: dict[str, Feature] = {}
    for number, payload in _iter_fields(record):
        if number != 1:  # Example.features
            continue
        for entry_number, entry in _iter_fields(payload):
            if entry_number != 1:  # Features.feature map entry
                continue
            name, feature = "", b""
            for map_number, map_value in _iter_fields(entry):
                if map_number == 1:
                    name = map_value.decode()
                elif map_number == 2:
                    feature = map_value
            out[name] = _parse_feature(feature)
    return out


def _parse_feature(buf: bytes) -> Feature:
    for number, payload in _iter_fields(buf):
        if number == 1:  # BytesList
            return Feature(BYTES, [v for f, v in _iter_fields(payload) if f == 1])
        if number == 2:  # FloatList
            inner = b"".join(v for f, v in _iter_fields(payload) if f == 1)
            return Feature(FLOAT, list(struct.unpack(f"<{len(inner) // 4}f", inner)))
        if number == 3:  # Int64List
            inner = b"".join(v for f, v in _iter_fields(payload) if f == 1)
            values: list[Any] = []
            pos = 0
            while pos < len(inner):
                value, pos = _read_varint(inner, pos)
                values.append(value)
            return Feature(INT64, values)
    return Feature(BYTES, [])
