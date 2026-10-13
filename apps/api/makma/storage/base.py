"""Storage abstraction.

Large media never lives in Postgres and never round-trips through the
frontend server unnecessarily. Keys follow a fixed prefix layout so a local
filesystem backend and an S3 backend are interchangeable.
"""
from __future__ import annotations

import abc
from pathlib import Path
from typing import BinaryIO, Iterable


class StorageKey:
    """Canonical key layout.

    originals/{project}/{asset}/{filename}
    proxies/{project}/{asset}/proxy.mp4
    thumbnails/{project}/{asset}/{name}.jpg
    waveforms/{project}/{asset}/waveform.json
    renders/{project}/{job}/{name}.mp4
    exports/{project}/{job}/{name}.mp4
    generated/{project}/{asset}/{name}
    """

    @staticmethod
    def original(project_id: str, asset_id: str, filename: str) -> str:
        return f"originals/{project_id}/{asset_id}/{filename}"

    @staticmethod
    def proxy(project_id: str, asset_id: str) -> str:
        return f"proxies/{project_id}/{asset_id}/proxy.mp4"

    @staticmethod
    def audio_proxy(project_id: str, asset_id: str) -> str:
        return f"proxies/{project_id}/{asset_id}/audio.m4a"

    @staticmethod
    def thumbnail(project_id: str, asset_id: str, name: str = "poster") -> str:
        return f"thumbnails/{project_id}/{asset_id}/{name}.jpg"

    @staticmethod
    def waveform(project_id: str, asset_id: str) -> str:
        return f"waveforms/{project_id}/{asset_id}/waveform.json"

    @staticmethod
    def render(project_id: str, job_id: str, name: str) -> str:
        return f"renders/{project_id}/{job_id}/{name}"


class Storage(abc.ABC):
    @abc.abstractmethod
    def write_stream(self, key: str, stream: BinaryIO) -> int:
        """Persist a stream, returning bytes written."""

    @abc.abstractmethod
    def write_bytes(self, key: str, data: bytes) -> int: ...

    @abc.abstractmethod
    def read_bytes(self, key: str) -> bytes: ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...

    @abc.abstractmethod
    def delete_prefix(self, prefix: str) -> int: ...

    @abc.abstractmethod
    def size(self, key: str) -> int: ...

    @abc.abstractmethod
    def local_path(self, key: str) -> Path:
        """A path FFmpeg can read.

        Local backend returns the real path; a remote backend must download
        to a temp file. Callers treat the result as read-only.
        """

    @abc.abstractmethod
    def stage_output(self, key: str) -> Path:
        """A writable local path that `commit_output` will publish."""

    @abc.abstractmethod
    def commit_output(self, key: str, path: Path) -> None: ...

    @abc.abstractmethod
    def url(self, key: str) -> str:
        """A URL the browser can fetch (signed where the backend supports it)."""

    @abc.abstractmethod
    def list_prefix(self, prefix: str) -> Iterable[str]: ...
