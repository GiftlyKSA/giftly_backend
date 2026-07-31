"""The object-storage contract (SPEC SECTION 16.1, 17.3).

Zero-proxy media: the API never streams bytes. It issues a pre-signed PUT URL, the
client uploads straight to S3, then confirms the key. Reads go through short-TTL
signed CDN URLs generated only after an ownership check. Services depend on this ABC;
only the Real/Fake implementations know the S3/CloudFront wire format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ObjectHead:
    """The result of heading a stored object."""

    exists: bool
    byte_size: int
    content_type: str


class StorageClient(ABC):
    """Issues pre-signed uploads, heads objects, and signs read URLs."""

    @abstractmethod
    async def create_upload_url(
        self, *, storage_key: str, content_type: str, byte_size: int, ttl_seconds: int
    ) -> str:
        """Return a pre-signed PUT URL pinned to a content-type and exact size."""

    @abstractmethod
    async def head_object(self, storage_key: str) -> ObjectHead | None:
        """Return object metadata, or None if it does not exist."""

    @abstractmethod
    async def verify_image_magic_bytes(self, storage_key: str) -> bool:
        """Return whether the object's real content is a supported image (magic bytes)."""

    @abstractmethod
    def signed_read_url(self, storage_key: str, *, ttl_seconds: int) -> str:
        """Return a short-TTL signed CDN read URL for an object."""
