"""The completion marker every source adapter writes into `raw/<source>/<upstream_id>/`.

Its presence is what makes `fetch` idempotent: written last, after the payload is durable, so
a crash before it leaves a directory the next run redoes from scratch.

It also carries the adapter version, and that is the part worth explaining. `raw/` holds
*upstream bytes*, which never change — but the **layout** of a staging directory is the
adapter's own format, and it changes when the adapter does. M4 hit both halves of this: a
staging written by `epic@1.0.0` held one merged `imu.json` that `epic@1.1.0` could not read,
and a `lerobot@1.0.0` staging held 0 rows because of a bug that `lerobot@1.1.0` fixed. In both
cases the marker said "done" and no re-run could ever repair it. So a version change
invalidates a staging exactly like it invalidates `normalized/`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rdp.infrastructure.storage.atomic_fs import atomic_write_text

STAGED_MARKER = ".staged.json"


def is_staged(dest: Path, adapter_version: str) -> bool:
    marker = dest / STAGED_MARKER
    if not marker.exists():
        return False
    staged: dict[str, Any] = json.loads(marker.read_text())
    return staged.get("adapter_version") == adapter_version


def mark_staged(dest: Path, adapter_version: str, **facts: Any) -> None:
    """Write the marker LAST. Everything it describes must already be on disk."""
    atomic_write_text(
        dest / STAGED_MARKER,
        json.dumps({**facts, "adapter_version": adapter_version}, sort_keys=True),
    )
