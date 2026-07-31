"""Admin JSON actions that move money (SPEC SECTION 20.H).

Distinct from the HTML admin dashboard (session-cookie auth, read-only for money):
these are JWT-authenticated actions requiring the ADMIN role. Dispute resolution and
withdrawal settlement live with ledger services and are guarded here by role.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Actor, get_db, get_settings, require_role
from app.core.exceptions import ValidationDomainError
from app.core.money import money_str, parse_money
from app.models import Withdrawal
from app.models.enums import DisputeStatus, UserRole
from app.repositories.audit_repository import AuditRepository
from app.repositories.dispute_repository import DisputeRepository
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.wallet_repository import WalletRepository
from app.repositories.withdrawal_repository import WithdrawalRepository
from app.schemas.fulfillment import DisputeResponse, ResolveDisputeRequest
from app.schemas.wallets import RejectWithdrawalRequest, WithdrawalResponse
from app.services.fulfillment_service import FulfillmentService
from app.services.media_service import MediaService
from app.services.money_service import MoneyService
from app.services.withdrawal_service import WithdrawalService

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


def _withdrawals(request: Request, db: AsyncSession) -> WithdrawalService:
    wallets = WalletRepository(db)
    return WithdrawalService(
        withdrawals=WithdrawalRepository(db),
        wallets=wallets,
        money=MoneyService(wallets),
        audit=AuditRepository(db),
        settings=get_settings(request),
    )


def _withdrawal_response(row: Withdrawal) -> WithdrawalResponse:
    return WithdrawalResponse(
        id=str(row.id),
        amount=money_str(row.amount),
        iban_last4=row.iban_last4,
        status=str(row.status),
        rejection_reason=row.rejection_reason,
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


@router.post("/withdrawals/{withdrawal_id}/approve", response_model=WithdrawalResponse)
async def approve_withdrawal(
    request: Request,
    db: DbDep,
    withdrawal_id: uuid.UUID,
    admin: Annotated[Actor, Depends(_Admin)],
) -> WithdrawalResponse:
    """Approve a requested withdrawal while retaining its funds hold."""
    row = await _withdrawals(request, db).approve(
        withdrawal_id=withdrawal_id,
        admin_id=admin.id,
        ip=request.client.host if request.client else None,
    )
    return _withdrawal_response(row)


@router.post("/withdrawals/{withdrawal_id}/reject", response_model=WithdrawalResponse)
async def reject_withdrawal(
    request: Request,
    db: DbDep,
    withdrawal_id: uuid.UUID,
    body: RejectWithdrawalRequest,
    admin: Annotated[Actor, Depends(_Admin)],
) -> WithdrawalResponse:
    """Reject a requested or approved withdrawal and release its funds."""
    row = await _withdrawals(request, db).reject(
        withdrawal_id=withdrawal_id,
        admin_id=admin.id,
        reason=body.reason,
        ip=request.client.host if request.client else None,
    )
    return _withdrawal_response(row)


@router.post("/withdrawals/{withdrawal_id}/paid", response_model=WithdrawalResponse)
async def mark_withdrawal_paid(
    request: Request,
    db: DbDep,
    withdrawal_id: uuid.UUID,
    admin: Annotated[Actor, Depends(_Admin)],
) -> WithdrawalResponse:
    """Settle an approved external payout through the append-only ledger."""
    row = await _withdrawals(request, db).mark_paid(
        withdrawal_id=withdrawal_id,
        admin_id=admin.id,
        ip=request.client.host if request.client else None,
    )
    return _withdrawal_response(row)
