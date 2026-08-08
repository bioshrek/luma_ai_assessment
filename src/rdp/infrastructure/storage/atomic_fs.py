"""Crash-safe writes: tmp file -> fsync file -> `os.replace` -> fsync directory.

`os.replace` is atomic within a filesystem, so a reader never observes a half-written artifact.
The directory fsync is what makes the *rename itself* durable — without it a power loss can
resurrect the old name even though the data blocks survived.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path


def fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, write: Callable[[Path], None]) -> Path:
    """Call `write` against a temporary sibling, then publish it under `path` atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        write(tmp)
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    def write(tmp: Path) -> None:
        tmp.write_bytes(data)

    return atomic_write(path, write)


def atomic_write_text(path: Path, text: str) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"))
