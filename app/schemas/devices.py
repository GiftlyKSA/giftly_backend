"""Pydantic contracts for device-token registration (SPEC SECTION 13)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class RegisterDeviceRequest(BaseModel):
    """Register (or refresh) a push token for the caller's device."""

    model_config = ConfigDict(extra="forbid")
    token: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    device_os: str = Field(..., description="IOS or ANDROID.")


class UnregisterDeviceRequest(BaseModel):
    """Remove a push token."""

    model_config = ConfigDict(extra="forbid")
    token: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class DeviceResponse(BaseModel):
    """A registered device token."""

    token: str
    device_os: str
