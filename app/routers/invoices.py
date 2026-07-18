"""Invoice routes (SPEC SECTION 11, 14).

The courier authors an invoice; the platform prices it. Reads are open to the order's
participants (customer or courier) and 404 to anyone else — no existence leak. The actor
id always comes from the JWT, never the body.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Actor, get_db, get_settings, require_role
from app.core.money import money_str, parse_money, parse_rate
from app.models import Invoice, InvoiceItem
from app.models.enums import UserRole
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.promo_repository import PromoRepository
from app.schemas.invoices import (
    CreateInvoiceRequest,
    InvoiceItemResponse,
    InvoiceResponse,
)
from app.services.invoice_service import InvoiceLineInput, InvoiceService, NewInvoiceInput
from app.services.promo_service import PromoService

router = APIRouter(prefix="/api", tags=["invoices"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
_Courier = require_role(UserRole.COURIER)
_Participant = require_role(UserRole.CUSTOMER, UserRole.COURIER)


def _service(request: Request, db: AsyncSession) -> InvoiceService:
    return InvoiceService(
        invoices=InvoiceRepository(db),
        orders=OrderRepository(db),
        promos=PromoService(PromoRepository(db)),
        settings=get_settings(request),
    )


def _item(item: InvoiceItem) -> InvoiceItemResponse:
    return InvoiceItemResponse(
        position=item.position,
        title=item.title,
        description=item.description,
        unit_price_amount=money_str(item.unit_price_amount),
        quantity=item.quantity,
        tax_rate=f"{item.tax_rate:.4f}",
        line_net_amount=money_str(item.line_net_amount),
        line_discount_amount=money_str(item.line_discount_amount),
        line_taxable_amount=money_str(item.line_taxable_amount),
        line_tax_amount=money_str(item.line_tax_amount),
        line_total_amount=money_str(item.line_total_amount),
    )


def _detail(invoice: Invoice, items: list[InvoiceItem]) -> InvoiceResponse:
    return InvoiceResponse(
        id=str(invoice.id),
        order_id=str(invoice.order_id),
        status=str(invoice.status),
        currency=invoice.currency,
        items_net_amount=money_str(invoice.items_net_amount),
        courier_fee_amount=money_str(invoice.courier_fee_amount),
        service_fee_amount=money_str(invoice.service_fee_amount),
        discount_amount=money_str(invoice.discount_amount),
        net_after_discount_amount=money_str(invoice.net_after_discount_amount),
        tax_amount=money_str(invoice.tax_amount),
        total_amount=money_str(invoice.total_amount),
        promo_code=invoice.promo_code_snapshot,
        issued_at=invoice.issued_at.isoformat() if invoice.issued_at else None,
        expires_at=invoice.expires_at.isoformat() if invoice.expires_at else None,
        items=[_item(i) for i in items],
    )


@router.post("/orders/{order_id}/invoices", response_model=InvoiceResponse, status_code=201)
async def create_invoice(
    request: Request,
    db: DbDep,
    order_id: uuid.UUID,
    body: CreateInvoiceRequest,
    actor: Annotated[Actor, Depends(_Courier)],
) -> InvoiceResponse:
    """Author and issue an invoice for an order (assigned courier only)."""
    data = NewInvoiceInput(
        items=[
            InvoiceLineInput(
                title=line.title,
                unit_price_amount=parse_money(line.unit_price_amount),
                quantity=line.quantity,
                tax_rate=parse_rate(line.tax_rate),
                description=line.description,
            )
            for line in body.items
        ],
        courier_fee_amount=parse_money(body.courier_fee_amount),
        promo_code=body.promo_code,
    )
    invoice = await _service(request, db).create_invoice(
        order_id=order_id, courier_id=actor.id, data=data
    )
    items = await InvoiceRepository(db).list_items(invoice.id)
    return _detail(invoice, items)


@router.get("/orders/{order_id}/invoice", response_model=InvoiceResponse)
async def get_order_invoice(
    request: Request,
    db: DbDep,
    order_id: uuid.UUID,
    actor: Annotated[Actor, Depends(_Participant)],
) -> InvoiceResponse:
    """Return an order's active invoice (participant only)."""
    invoice, items = await _service(request, db).get_active_invoice_for_order(
        order_id=order_id, actor_id=actor.id
    )
    return _detail(invoice, items)


@router.get("/invoices/{invoice_id}", response_model=InvoiceResponse)
async def get_invoice(
    request: Request,
    db: DbDep,
    invoice_id: uuid.UUID,
    actor: Annotated[Actor, Depends(_Participant)],
) -> InvoiceResponse:
    """Return an invoice by id (participant only)."""
    invoice, items = await _service(request, db).get_invoice_for_actor(
        invoice_id=invoice_id, actor_id=actor.id
    )
    return _detail(invoice, items)


@router.post("/invoices/{invoice_id}/cancel", response_model=InvoiceResponse)
async def cancel_invoice(
    request: Request,
    db: DbDep,
    invoice_id: uuid.UUID,
    actor: Annotated[Actor, Depends(_Courier)],
) -> InvoiceResponse:
    """Cancel an unpaid issued invoice, reopening the order (issuing courier only)."""
    invoice = await _service(request, db).cancel_invoice(invoice_id=invoice_id, courier_id=actor.id)
    items = await InvoiceRepository(db).list_items(invoice.id)
    return _detail(invoice, items)
