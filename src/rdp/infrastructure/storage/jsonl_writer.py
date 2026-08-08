"""JSONL export writer. One self-describing JSON object per episode, written atomically."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rdp.infrastructure.storage.atomic_fs import atomic_write_text


class JsonlSubsetWriter:
    def write(self, path: Path, records: Sequence[Mapping[str, Any]]) -> None:
        body = "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records
        )
        atomic_write_text(path, body)
