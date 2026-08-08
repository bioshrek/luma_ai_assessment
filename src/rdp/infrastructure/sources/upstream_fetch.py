"""Fetching upstream files.

Deliberately stdlib `urllib` rather than `huggingface-hub`: the only thing we need is "GET this
path at this revision", and a dependency that pulls in its own cache, auth and retry semantics
would be a large surface for a small need. `file://` and plain paths are supported too, so
tests and offline runs point the same adapter at a local fixture directory.
"""

from __future__ import annotations

import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import IO

from rdp.domain.source import Source
from rdp.infrastructure.storage.atomic_fs import atomic_write

HF_PREFIX = "hf://datasets/"
_ALLOWED_SCHEMES = ("https",)


class UpstreamNotFound(Exception):
    """The requested path does not exist upstream. Used for probing shard layouts."""


class UpstreamFetcher:
    def __init__(self, cache_root: Path, timeout: float = 60.0) -> None:
        self.cache_root = cache_root
        self.timeout = timeout

    def local_path(self, source: Source, rel_path: str) -> Path:
        """Return a local path for `rel_path`, downloading it once if the source is remote."""
        local_root = _local_root(source)
        if local_root is not None:
            candidate = local_root / rel_path
            if not candidate.exists():
                raise UpstreamNotFound(str(candidate))
            return candidate

        destination = self.cache_root / source.source_id / source.revision / rel_path
        if destination.exists():
            return destination
        url = _resolve_url(source, rel_path)
        self._download(url, destination)
        return destination

    def exists(self, source: Source, rel_path: str) -> bool:
        try:
            self.local_path(source, rel_path)
        except UpstreamNotFound:
            return False
        return True

    def _download(self, url: str, destination: Path) -> None:
        scheme = urllib.parse.urlparse(url).scheme
        if scheme not in _ALLOWED_SCHEMES:
            raise ValueError(f"refusing to fetch {url!r}: only {_ALLOWED_SCHEMES} are allowed")
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                atomic_write(destination, lambda tmp: _stream_to(response, tmp))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise UpstreamNotFound(url) from exc
            raise


def _stream_to(response: IO[bytes], path: Path) -> None:
    with path.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def _local_root(source: Source) -> Path | None:
    """A source may point at a directory on disk — a fixture, a mirror, or a manual download."""
    uri = source.uri
    if uri.startswith("file://"):
        return Path(urllib.parse.urlparse(uri).path)
    if "://" not in uri:
        return Path(uri)
    return None


def _resolve_url(source: Source, rel_path: str) -> str:
    if source.uri.startswith(HF_PREFIX):
        repo = source.uri[len(HF_PREFIX) :].strip("/")
        return f"https://huggingface.co/datasets/{repo}/resolve/{source.revision}/{rel_path}"
    return f"{source.uri.rstrip('/')}/{rel_path}"
