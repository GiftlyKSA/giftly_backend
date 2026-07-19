"""Admin JSON actions that move money (SPEC SECTION 20.H).

Distinct from the HTML admin dashboard (session-cookie auth, read-only for money):
these are JWT-authenticated actions requiring the ADMIN role. Dispute resolution moves
escrow, so it lives with the ledger services, guarded here by role.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Actor, get_db, get_settings, require_role
from app.core.exceptions import ValidationDomainError
from app.core.money import parse_money
from app.models.enums import DisputeStatus, UserRole
from app.repositories.dispute_repository import DisputeRepository
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.wallet_repository import WalletRepository
from app.schemas.fulfillment import DisputeResponse, ResolveDisputeRequest
from app.services.fulfillment_service import FulfillmentService
from app.services.media_service import MediaService
from app.services.money_service import MoneyService

router = APIRouter(prefix="/api/admin", tags=["admin-api"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
_Admin = require_role(UserRole.ADMIN)


def _fulfillment(request: Request, db: AsyncSession) -> FulfillmentService:
    return FulfillmentService(
        orders=OrderRepository(db),
        invoices=InvoiceRepository(db),
        disputes=DisputeRepository(db),
        wallets=WalletRepository(db),
        money=MoneyService(WalletRepository(db)),
        media=MediaService(request.app.state.clients.storage, get_settings(request)),
        settings=get_settings(request),
    )


@router.post("/disputes/{dispute_id}/resolve", response_model=DisputeResponse)
async def resolve_dispute(
    request: Request,
    db: DbDep,
    dispute_id: uuid.UUID,
    body: ResolveDisputeRequest,
    admin: Annotated[Actor, Depends(_Admin)],
) -> DisputeResponse:
    """Resolve a dispute, moving escrow accordingly (ADMIN only)."""
    try:
        outcome = DisputeStatus(body.outcome)
    except ValueError as exc:
        raise ValidationDomainError("Unknown dispute outcome.") from exc
    if outcome is DisputeStatus.OPEN:
        raise ValidationDomainError("A resolution outcome is required.")

    dispute = await _fulfillment(request, db).resolve_dispute(
        dispute_id=dispute_id,
        admin_id=admin.id,
        outcome=outcome,
        note=body.note,
        courier_amount=parse_money(body.courier_amount) if body.courier_amount else None,
    )
    return DisputeResponse(
        id=str(dispute.id),
        order_id=str(dispute.order_id),
        status=str(dispute.status),
        reason=dispute.reason,
        resolution_note=dispute.resolution_note,
    )
