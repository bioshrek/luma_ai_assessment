"""`CameraSpec` — camera topology and storage form (design §2.2e)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CameraMount(StrEnum):
    STATIC = "static"
    WRIST = "wrist"
    HEAD = "head"
    UNKNOWN = "unknown"


class CameraEncoding(StrEnum):
    MP4_SIDECAR = "mp4_sidecar"
    INLINE_FRAMES = "inline_frames"
    ABSENT = "absent"


class CameraSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    mount: CameraMount
    resolution: tuple[int, int]
    """(height, width)."""
    channels: int
    encoding: CameraEncoding
    is_present: bool
    """Whether the pixels are available locally — not merely declared upstream."""
    n_frames: int | None = None
    """Decoded frame count of this camera's imagery, *measured* by the adapter. `None` means the
    pixels were never fetched, which is not the same as zero — `VIDEO_FRAME_MISMATCH` skips on
    `None` and fires on a real disagreement."""
