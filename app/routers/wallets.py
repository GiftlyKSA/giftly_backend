"""Wallet routes (SPEC SECTION 19).

Reads are scoped to the authenticated user's own wallet — ownership is enforced by
filtering on the actor id from the JWT, never a path or body value.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Actor, get_db, require_role
from app.core.exceptions import NotFoundError
from app.core.money import money_str
from app.models.enums import UserRole
from app.repositories.wallet_repository import WalletRepository
from app.schemas.wallets import TransactionPage, TransactionResponse, WalletResponse

router = APIRouter(prefix="/api/wallets", tags=["wallets"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
_CustomerOrCourier = require_role(UserRole.CUSTOMER, UserRole.COURIER)


@router.get("/me", response_model=WalletResponse)
async def get_my_wallet(
    db: DbDep, actor: Annotated[Actor, Depends(_CustomerOrCourier)]
) -> WalletResponse:
    """Return the authenticated user's wallet snapshot."""
    wallet = await WalletRepository(db).get_by_user(actor.id)
    if wallet is None:
        raise NotFoundError("Wallet not found.")
    return WalletResponse(
        balance=money_str(wallet.balance),
        held_balance=money_str(wallet.held_balance),
        available=money_str(wallet.balance - wallet.held_balance),
        currency=wallet.currency,
    )


@router.get("/me/transactions", response_model=TransactionPage)
async def list_my_transactions(
    db: DbDep,
    actor: Annotated[Actor, Depends(_CustomerOrCourier)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TransactionPage:
    """Return the authenticated user's ledger entries, newest first (keyset paged)."""
    repo = WalletRepository(db)
    wallet = await repo.get_by_user(actor.id)
    if wallet is None:
        raise NotFoundError("Wallet not found.")
    before = uuid.UUID(cursor) if cursor else None
    rows = await repo.list_transactions(wallet.id, limit=limit, before_id=before)
    items = [
        TransactionResponse(
            id=str(t.id),
            amount=money_str(t.amount),
            type=str(t.type),
            status=str(t.status),
            balance_after=money_str(t.balance_after),
            created_at=t.created_at.isoformat(),
        )
        for t in rows
    ]
    next_cursor = str(rows[-1].id) if len(rows) == limit else None
    return TransactionPage(items=items, next_cursor=next_cursor)
