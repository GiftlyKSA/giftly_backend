"""Public promo routes (SPEC SECTION 12.2).

Only the customer-facing preview lives here: validate a promo against the customer's
own order and see the exact discount and resulting total. Reserving/consuming a promo
happens inside the invoice pipeline, never from a client call.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Actor, get_db, get_settings, require_role
from app.core.money import money_str
from app.models.enums import UserRole
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.promo_repository import PromoRepository
from app.schemas.invoices import PromoPreviewResponse, PromoValidateRequest
from app.services.invoice_service import InvoiceService
from app.services.promo_service import PromoService

router = APIRouter(prefix="/api/promos", tags=["promos"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
_Customer = require_role(UserRole.CUSTOMER)


def _service(request: Request, db: AsyncSession) -> InvoiceService:
    return InvoiceService(
        invoices=InvoiceRepository(db),
        orders=OrderRepository(db),
        promos=PromoService(PromoRepository(db)),
        settings=get_settings(request),
    )


@router.post("/validate", response_model=PromoPreviewResponse)
async def validate_promo(
    request: Request,
    db: DbDep,
    body: PromoValidateRequest,
    actor: Annotated[Actor, Depends(_Customer)],
) -> PromoPreviewResponse:
    """Preview a promo against the customer's own order's active invoice."""
    preview = await _service(request, db).preview_promo(
        order_id=uuid.UUID(body.order_id), code=body.code, customer_id=actor.id
    )
    return PromoPreviewResponse(
        code=preview.code,
        discount_amount=money_str(preview.discount_amount),
        original_total_amount=money_str(preview.original_total_amount),
        total_amount=money_str(preview.total_amount),
    )
