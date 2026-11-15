"""Object storage behind one small interface (ADR 0002).

Postgres keeps the pointer (object_key) and metadata; bytes live here. Adapters:
- LocalObjectStore: a folder on disk. Default for local development and tests.
- SupabaseObjectStore: Supabase Storage over HTTP (S3/Azure Blob/MinIO adapters follow the
  same interface, which is what keeps the app off any single vendor's blob store).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import httpx


class ObjectNotFound(Exception):
    pass


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...


class LocalObjectStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:          # block ../ path traversal
            raise ValueError("Invalid object key")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise ObjectNotFound(key)
        return path.read_bytes()


class SupabaseObjectStore:
    def __init__(self, url: str, service_key: str, bucket: str):
        self.base = f"{url.rstrip('/')}/storage/v1/object/{bucket}"
        self.headers = {"apikey": service_key}
        if not service_key.startswith("sb_"):
            self.headers["Authorization"] = f"Bearer {service_key}"

    def put(self, key: str, data: bytes, content_type: str) -> None:
        r = httpx.post(f"{self.base}/{key}", content=data, timeout=30,
                       headers={**self.headers, "Content-Type": content_type,
                                "x-upsert": "false"})
        r.raise_for_status()

    def get(self, key: str) -> bytes:
        r = httpx.get(f"{self.base}/{key}", headers=self.headers, timeout=30)
        if r.status_code in (400, 404):
            raise ObjectNotFound(key)
        r.raise_for_status()
        return r.content
