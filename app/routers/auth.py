"""Authentication routes (SPEC SECTION 19, 20.A).

These endpoints are public (in the PUBLIC_ROUTES sense): OTP request/verify, register,
refresh, and logout. Business logic lives in the auth service; the router only parses,
calls the service, and serializes.
"""

from __future__ import annotations

from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Actor, get_db, get_redis, get_settings, require_auth
from app.core.jwt import create_access_token  # noqa: F401 — kept for symmetry/testing
from app.models.enums import UserRole
from app.repositories.auth_repository import AuthRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import (
    RefreshRequest,
    RegisterRequest,
    SendOtpRequest,
    SendOtpResponse,
    TokenResponse,
    VerifyOtpRequest,
    VerifyOtpResponse,
)
from app.services.auth_service import AuthService
from app.services.otp_service import OtpService

router = APIRouter(prefix="/api/auth", tags=["auth"])

DbDep = Annotated[AsyncSession, Depends(get_db)]


def _service(request: Request, db: AsyncSession) -> AuthService:
    settings = get_settings(request)
    redis = get_redis(request)
    otp = OtpService(redis, request.app.state.clients.sms, settings)
    return AuthService(
        settings=settings,
        redis=redis,
        otp=otp,
        users=UserRepository(db),
        auth_repo=AuthRepository(db),
        session=db,
    )


@router.post("/send-otp", response_model=SendOtpResponse, status_code=202)
async def send_otp(request: Request, db: DbDep, body: SendOtpRequest) -> SendOtpResponse:
    """Send a login OTP. The response is identical whether or not the phone exists."""
    expires_in, dev_code = await _service(request, db).send_otp(body.phone)
    return SendOtpResponse(expires_in=expires_in, dev_otp=dev_code)


@router.post("/verify-otp", response_model=VerifyOtpResponse, status_code=200)
async def verify_otp(request: Request, db: DbDep, body: VerifyOtpRequest) -> VerifyOtpResponse:
    """Verify an OTP, returning tokens for an existing user or a registration token."""
    result = await _service(request, db).verify_otp(body.phone, body.otp)
    if result.tokens is not None:
        return VerifyOtpResponse(
            is_new_user=False,
            role=result.tokens.role,
            access_token=result.tokens.access_token,
            refresh_token=result.tokens.refresh_token,
        )
    return VerifyOtpResponse(is_new_user=True, registration_token=result.registration_token)


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(request: Request, db: DbDep, body: RegisterRequest) -> TokenResponse:
    """Create the account authorised by a registration token and return tokens."""
    tokens = await _service(request, db).register(
        registration_token=body.registration_token,
        role=UserRole(body.role),
        full_name=body.full_name,
        email=body.email,
        dob=body.dob,
        city=body.city,
        national_id=body.national_id,
        passport_id=body.passport_id,
    )
    return TokenResponse(
        access_token=tokens.access_token, refresh_token=tokens.refresh_token, role=tokens.role
    )


@router.post("/refresh", response_model=TokenResponse, status_code=200)
async def refresh(request: Request, db: DbDep, body: RefreshRequest) -> TokenResponse:
    """Rotate a refresh token, returning a new pair (reuse revokes the family)."""
    tokens = await _service(request, db).refresh(body.refresh_token)
    return TokenResponse(
        access_token=tokens.access_token, refresh_token=tokens.refresh_token, role=tokens.role
    )


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    db: DbDep,
    actor: Annotated[Actor, Depends(require_auth)],
) -> Response:
    """Denylist the caller's access token until it would have expired."""
    from app.core.jwt import decode_access_token

    token = request.headers.get("Authorization", "")[len("Bearer ") :].strip()
    claims = decode_access_token(get_settings(request), token)
    from datetime import datetime

    remaining = claims.exp - int(datetime.now(UTC).timestamp())
    await _service(request, db).logout(jti=actor.jti, remaining_ttl_seconds=remaining)
    return Response(status_code=204)
