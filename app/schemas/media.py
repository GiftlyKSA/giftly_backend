"""Pydantic contracts for the media endpoints (SPEC SECTION 19)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UploadUrlRequest(BaseModel):
    """Request a pre-signed upload URL for a media object."""

    model_config = ConfigDict(extra="forbid")
    purpose: Literal["ORDER_REQUEST", "DELIVERY_PROOF"] = Field(
        ..., description="What the media is for; determines the key prefix."
    )
    content_type: Literal["image/jpeg", "image/png"] = Field(
        ..., description="The image MIME type; pinned into the pre-signed URL."
    )
    byte_size: int = Field(..., gt=0, description="Declared size in bytes; must be within the cap.")


class UploadUrlResponse(BaseModel):
    """A pre-signed upload URL and the server-generated key."""

    upload_url: str = Field(..., description="PUT the bytes here directly (never via the API).")
    storage_key: str = Field(..., description="Confirm this key after uploading.")
    expires_in: int = Field(..., description="Seconds the URL is valid.")


class ConfirmRequest(BaseModel):
    """Confirm that an object was uploaded to a key."""

    model_config = ConfigDict(extra="forbid")
    storage_key: str = Field(..., description="The key returned by upload-urls.")


class ConfirmResponse(BaseModel):
    """Confirmation that an upload passed validation."""

    storage_key: str = Field(..., description="The confirmed key.")
    confirmed: bool = Field(..., description="True when the object is valid.")
