"""Media routes (SPEC SECTION 19).

Any authenticated user may request an upload URL and confirm it. The bytes never pass
through the API — the client PUTs straight to S3 with the pre-signed URL.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.deps import Actor, require_auth
from app.integrations.storage.base import StorageClient
from app.schemas.media import (
    ConfirmRequest,
    ConfirmResponse,
    UploadUrlRequest,
    UploadUrlResponse,
)
from app.services.media_service import MediaService

router = APIRouter(prefix="/api/media", tags=["media"])


def _service(request: Request) -> MediaService:
    storage: StorageClient = request.app.state.clients.storage
    return MediaService(storage, request.app.state.settings)


@router.post("/upload-urls", response_model=UploadUrlResponse, status_code=201)
async def create_upload_url(
    request: Request,
    body: UploadUrlRequest,
    _actor: Annotated[Actor, Depends(require_auth)],
) -> UploadUrlResponse:
    """Issue a pre-signed upload URL with a server-generated key."""
    url, key, expires_in = await _service(request).request_upload_url(
        purpose=body.purpose, content_type=body.content_type, byte_size=body.byte_size
    )
    return UploadUrlResponse(upload_url=url, storage_key=key, expires_in=expires_in)


@router.post("/confirm", response_model=ConfirmResponse, status_code=200)
async def confirm_upload(
    request: Request,
    body: ConfirmRequest,
    _actor: Annotated[Actor, Depends(require_auth)],
) -> ConfirmResponse:
    """Confirm an uploaded object exists and is a valid image."""
    await _service(request).confirm(body.storage_key)
    return ConfirmResponse(storage_key=body.storage_key, confirmed=True)
