"""User profile routes (SPEC SECTION 19).

The client fetches profile data here rather than from the JWT (which carries only
ids), so edits take effect immediately. Ownership is implicit: the actor id comes from
the verified token, never the path or body.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Actor, get_db, require_auth
from app.core.exceptions import NotFoundError
from app.repositories.user_repository import UserRepository
from app.schemas.users import UserMeResponse, UserUpdateRequest

router = APIRouter(prefix="/api/users", tags=["users"])

DbDep = Annotated[AsyncSession, Depends(get_db)]


@router.get("/me", response_model=UserMeResponse)
async def get_me(db: DbDep, actor: Annotated[Actor, Depends(require_auth)]) -> UserMeResponse:
    """Return the authenticated user's own profile."""
    user = await UserRepository(db).get(actor.id)
    if user is None:
        raise NotFoundError("User not found.")
    return _to_response(user)


@router.patch("/me", response_model=UserMeResponse)
async def update_me(
    request: Request,
    db: DbDep,
    body: UserUpdateRequest,
    actor: Annotated[Actor, Depends(require_auth)],
) -> UserMeResponse:
    """Update the authenticated user's editable profile fields."""
    repo = UserRepository(db)
    user = await repo.get(actor.id)
    if user is None:
        raise NotFoundError("User not found.")
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.email is not None:
        user.email = body.email
    if body.dob is not None:
        user.date_of_birth = body.dob
    await db.flush()
    return _to_response(user)


def _to_response(user: object) -> UserMeResponse:
    return UserMeResponse(
        id=str(user.id),  # type: ignore[attr-defined]
        phone=user.phone,  # type: ignore[attr-defined]
        role=str(user.role),  # type: ignore[attr-defined]
        status=str(user.status),  # type: ignore[attr-defined]
        full_name=user.full_name,  # type: ignore[attr-defined]
        email=user.email,  # type: ignore[attr-defined]
        rating=str(user.rating),  # type: ignore[attr-defined]
        rating_count=user.rating_count,  # type: ignore[attr-defined]
    )
