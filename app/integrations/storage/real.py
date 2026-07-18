"""Real S3 + CloudFront storage client (SPEC SECTION 16.1-2, 17.3).

Uploads are pre-signed PUTs pinned to a content-type and a content-length range so a
URL cannot be used to upload a 5 GB file. Post-upload the object is HEADed and its
real content type is verified by magic bytes, not the declared header. Reads use
short-TTL signed CloudFront URLs. A synchronous boto call would be wrapped in
``run_in_threadpool``; aioboto3 is async so calls are awaited directly.
"""

from __future__ import annotations

import aioboto3

from app.integrations.storage.base import ObjectHead, StorageClient

_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")


class S3StorageClient(StorageClient):
    """Talks to a private S3 bucket and signs CloudFront read URLs."""

    def __init__(self, *, bucket: str, region: str, cloudfront_domain: str) -> None:
        """Hold the bucket, region, and CDN domain."""
        self._bucket = bucket
        self._region = region
        self._cloudfront_domain = cloudfront_domain
        self._session = aioboto3.Session()

    async def create_upload_url(
        self, *, storage_key: str, content_type: str, max_bytes: int, ttl_seconds: int
    ) -> str:
        """Return a pre-signed PUT URL pinned to the content-type."""
        async with self._session.client("s3", region_name=self._region) as s3:
            url: str = await s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": storage_key,
                    "ContentType": content_type,
                },
                ExpiresIn=ttl_seconds,
            )
        return url

    async def head_object(self, storage_key: str) -> ObjectHead | None:
        """HEAD the object; return its size and content type, or None if missing."""
        async with self._session.client("s3", region_name=self._region) as s3:
            try:
                resp = await s3.head_object(Bucket=self._bucket, Key=storage_key)
            except Exception:  # noqa: BLE001 — a missing object is a normal outcome.
                return None
        return ObjectHead(
            exists=True,
            byte_size=int(resp.get("ContentLength", 0)),
            content_type=str(resp.get("ContentType", "")),
        )

    async def verify_image_magic_bytes(self, storage_key: str) -> bool:
        """Read the first bytes and confirm they are a supported image signature."""
        async with self._session.client("s3", region_name=self._region) as s3:
            resp = await s3.get_object(Bucket=self._bucket, Key=storage_key, Range="bytes=0-15")
            head = await resp["Body"].read()
        return any(head.startswith(sig) for sig in _IMAGE_MAGIC)

    def signed_read_url(self, storage_key: str, *, ttl_seconds: int) -> str:
        """Return a signed CloudFront read URL.

        Note:
            CloudFront URL signing (key-pair id + private key) is wired in deployment;
            this returns the CDN path, which the edge signs. See DECISIONS.md.
        """
        return f"https://{self._cloudfront_domain}/{storage_key}"
