"""Real S3 + CloudFront storage client (SPEC SECTION 16.1-2, 17.3).

Uploads are pre-signed PUTs pinned to a content-type and exact content length so a
URL cannot be used to upload a larger object. Post-upload the object is HEADed and its
real content type is verified by magic bytes, not the declared header. Reads use
short-TTL signed CloudFront URLs. A synchronous boto call would be wrapped in
``run_in_threadpool``; aioboto3 is async so calls are awaited directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.signers import CloudFrontSigner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app.integrations.storage.base import ObjectHead, StorageClient

_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")


class S3StorageClient(StorageClient):
    """Talks to a private S3 bucket and signs CloudFront read URLs."""

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        cloudfront_domain: str,
        cloudfront_key_pair_id: str,
        cloudfront_private_key: str,
    ) -> None:
        """Hold the bucket, region, and CDN domain."""
        self._bucket = bucket
        self._region = region
        self._cloudfront_domain = cloudfront_domain
        self._cloudfront_key_pair_id = cloudfront_key_pair_id
        key = serialization.load_pem_private_key(
            cloudfront_private_key.replace("\\n", "\n").encode("utf-8"), password=None
        )
        if not isinstance(key, RSAPrivateKey):
            raise ValueError("CLOUDFRONT_PRIVATE_KEY must contain an RSA private key.")
        self._cloudfront_private_key = key
        self._client_config = Config(signature_version="s3v4")
        self._session = aioboto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )

    async def create_upload_url(
        self, *, storage_key: str, content_type: str, byte_size: int, ttl_seconds: int
    ) -> str:
        """Return a pre-signed PUT URL pinned to the content-type."""
        async with self._session.client(
            "s3", region_name=self._region, config=self._client_config
        ) as s3:
            url: str = await s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": storage_key,
                    "ContentType": content_type,
                    "ContentLength": byte_size,
                },
                ExpiresIn=ttl_seconds,
            )
        return url

    async def head_object(self, storage_key: str) -> ObjectHead | None:
        """HEAD the object; return its size and content type, or None if missing."""
        async with self._session.client(
            "s3", region_name=self._region, config=self._client_config
        ) as s3:
            try:
                resp = await s3.head_object(Bucket=self._bucket, Key=storage_key)
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code in {"404", "NoSuchKey", "NotFound"}:
                    return None
                raise
        return ObjectHead(
            exists=True,
            byte_size=int(resp.get("ContentLength", 0)),
            content_type=str(resp.get("ContentType", "")),
        )

    async def verify_image_magic_bytes(self, storage_key: str) -> bool:
        """Read the first bytes and confirm they are a supported image signature."""
        async with self._session.client(
            "s3", region_name=self._region, config=self._client_config
        ) as s3:
            resp = await s3.get_object(Bucket=self._bucket, Key=storage_key, Range="bytes=0-15")
            head = await resp["Body"].read()
        return any(head.startswith(sig) for sig in _IMAGE_MAGIC)

    def signed_read_url(self, storage_key: str, *, ttl_seconds: int) -> str:
        """Return a short-lived, RSA-signed CloudFront read URL."""
        signer = CloudFrontSigner(self._cloudfront_key_pair_id, self._sign_cloudfront_policy)
        signed = cast(
            str,
            signer.generate_presigned_url(
                f"https://{self._cloudfront_domain}/{storage_key}",
                date_less_than=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            ),
        )
        return f"{signed}&Hash-Algorithm=SHA256"

    def _sign_cloudfront_policy(self, message: bytes) -> bytes:
        """Sign a CloudFront canned policy using AWS's documented algorithm."""
        return self._cloudfront_private_key.sign(
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
