"""Filesystem storage backend (default for local development)."""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import time
from pathlib import Path
from typing import BinaryIO, Iterable

from ..config import settings
from .base import Storage


#: How long a media link stays valid. Long enough that a browser tab keeps
#: working through an editing session, short enough that a leaked URL stops
#: mattering. Production halves it because links there travel further.
DEFAULT_URL_TTL = 6 * 3600
PRODUCTION_URL_TTL = 30 * 60


def default_ttl() -> int:
    return PRODUCTION_URL_TTL if settings.is_production else DEFAULT_URL_TTL


def sign_key(key: str, ttl_seconds: int | None = None) -> tuple[str, str]:
    """Return (expiry, signature) for a media URL.

    Media is private by default; the browser gets a time-limited signed URL
    rather than a publicly listable path. The signature binds the URL to one
    key *and* one expiry, so neither can be edited without invalidating it.
    """
    ttl_seconds = default_ttl() if ttl_seconds is None else ttl_seconds
    expiry = str(int(time.time()) + ttl_seconds)
    msg = f"{key}:{expiry}".encode()
    sig = hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return expiry, sig


def verify_key(key: str, expiry: str, signature: str) -> bool:
    try:
        if int(expiry) < time.time():
            return False
    except (TypeError, ValueError):
        return False
    msg = f"{key}:{expiry}".encode()
    expected = hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, signature or "")


class LocalStorage(Storage):
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.storage_root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- internals ------------------------------------------------------
    def _path(self, key: str) -> Path:
        clean = key.replace("\\", "/").lstrip("/")
        if ".." in clean.split("/"):
            raise ValueError(f"Unsafe storage key: {key}")
        path = (self.root / clean).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError(f"Storage key escapes root: {key}")
        return path

    # -- Storage API ----------------------------------------------------
    def write_stream(self, key: str, stream: BinaryIO) -> int:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with open(path, "wb") as fh:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
        return written

    def write_bytes(self, key: str, data: bytes) -> int:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return len(data)

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def delete_prefix(self, prefix: str) -> int:
        path = self._path(prefix)
        if not path.exists():
            return 0
        count = sum(1 for _ in path.rglob("*") if _.is_file())
        shutil.rmtree(path, ignore_errors=True)
        return count

    def size(self, key: str) -> int:
        path = self._path(key)
        return path.stat().st_size if path.exists() else 0

    def local_path(self, key: str) -> Path:
        return self._path(key)

    def stage_output(self, key: str) -> Path:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def commit_output(self, key: str, path: Path) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.resolve() != target.resolve():
            shutil.move(str(path), str(target))

    def url(self, key: str) -> str:
        expiry, sig = sign_key(key)
        return f"/api/media/{key}?e={expiry}&s={sig}"

    def list_prefix(self, prefix: str) -> Iterable[str]:
        base = self._path(prefix)
        if not base.exists():
            return []
        return [
            str(p.relative_to(self.root)).replace(os.sep, "/")
            for p in base.rglob("*")
            if p.is_file()
        ]


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        if settings.storage_backend == "local":
            _storage = LocalStorage()
        else:  # pragma: no cover - S3 backend lands with cloud deployment
            raise NotImplementedError(
                f"Storage backend '{settings.storage_backend}' is not implemented "
                "yet. Set MAKMA_STORAGE_BACKEND=local."
            )
    return _storage
