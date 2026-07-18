"""In-memory storage double (SPEC SECTION 5.1, 16.1).

Lets the full media + order flow run with no S3. ``create_upload_url`` registers the
key so a later ``head_object`` reports it, mirroring a completed client PUT; magic-byte
verification is reported valid (there are no real bytes to inspect in the fake).
"""

from __future__ import annotations

from app.core.config import Environment
from app.integrations._guard import forbid_in_production
from app.integrations.storage.base import ObjectHead, StorageClient


class FakeStorageClient(StorageClient):
    """Records pre-signed keys in memory and reports them as uploaded."""

    def __init__(self, environment: Environment) -> None:
        """Refuse construction in production, then start with an empty store."""
        forbid_in_production(environment, type(self).__name__)
        self._objects: dict[str, ObjectHead] = {}

    async def create_upload_url(
        self, *, storage_key: str, content_type: str, max_bytes: int, ttl_seconds: int
    ) -> str:
        """Register the key as uploaded and return a local simulate URL."""
        self._objects[storage_key] = ObjectHead(
            exists=True, byte_size=min(max_bytes, 1_843_200), content_type=content_type
        )
        return f"http://localhost:8000/dev/upload/{storage_key}"

    async def head_object(self, storage_key: str) -> ObjectHead | None:
        """Return the recorded object metadata, or None."""
        return self._objects.get(storage_key)

    async def verify_image_magic_bytes(self, storage_key: str) -> bool:
        """Report a recorded object as a valid image."""
        return storage_key in self._objects

    def signed_read_url(self, storage_key: str, *, ttl_seconds: int) -> str:
        """Return a deterministic fake CDN URL."""
        return f"http://localhost:8000/dev/cdn/{storage_key}?ttl={ttl_seconds}"
