"""Device-token routes (SPEC SECTION 13).

A user registers a push token per device; the token is unique across users, so
registering re-points a handed-down device away from its previous owner. The actor id
comes from the JWT, never the body.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Actor, get_db, require_role
from app.core.exceptions import ValidationDomainError
from app.models.enums import DeviceOs, UserRole
from app.repositories.device_token_repository import DeviceTokenRepository
from app.schemas.devices import (
    DeviceResponse,
    RegisterDeviceRequest,
    UnregisterDeviceRequest,
)

router = APIRouter(prefix="/api/devices", tags=["devices"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
_User = require_role(UserRole.CUSTOMER, UserRole.COURIER)


@router.post("", response_model=DeviceResponse, status_code=201)
async def register_device(
    db: DbDep, body: RegisterDeviceRequest, actor: Annotated[Actor, Depends(_User)]
) -> DeviceResponse:
    """Register or refresh a push token for the caller."""
    try:
        device_os = DeviceOs(body.device_os)
    except ValueError as exc:
        raise ValidationDomainError("device_os must be IOS or ANDROID.") from exc
    row = await DeviceTokenRepository(db).register(
        user_id=actor.id, token=body.token, device_os=device_os
    )
    return DeviceResponse(token=row.token, device_os=str(row.device_os))


@router.delete("", status_code=204)
async def unregister_device(
    db: DbDep, body: UnregisterDeviceRequest, actor: Annotated[Actor, Depends(_User)]
) -> None:
    """Remove a push token (only if it belongs to the caller)."""
    await DeviceTokenRepository(db).remove(user_id=actor.id, token=body.token)
