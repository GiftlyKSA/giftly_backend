"""Admin dashboard routes (SPEC SECTION 18.3).

Server-rendered Jinja pages mounted at ``/admin``. Every route calls the admin
services and never queries the DB directly. Reads are open to any authenticated
admin; every mutating action requires CSRF, writes an audit row, and — for revealing
Restricted data — a fresh step-up grant. Money-moving resolutions (disputes,
withdrawals) are shown read-only; they belong to the ledger service.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import (
    SESSION_COOKIE,
    AdminContext,
    build_auth_service,
    client_ip,
    get_db,
    get_redis_from,
    get_settings_from,
    require_admin,
    require_step_up,
    verify_csrf,
)
from app.models.enums import PromoDiscountType

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DbDep = Annotated[AsyncSession, Depends(get_db)]


def _render(request: Request, template: str, **context: object) -> HTMLResponse:
    """Render a template with the request bound."""
    return _TEMPLATES.TemplateResponse(request, template, {"request": request, **context})


async def _ctx(request: Request, db: AsyncSession) -> AdminContext:
    return await require_admin(request, db)


# --- Authentication ----------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Show the admin login form (phone entry)."""
    return _render(request, "login.html", stage="phone")


@router.post("/login", response_class=HTMLResponse)
async def login_request_otp(
    request: Request, db: DbDep, phone: Annotated[str, Form()]
) -> HTMLResponse:
    """Send a login OTP and show the code-entry stage."""
    auth = build_auth_service(db, get_redis_from(request), get_settings_from(request), request)
    dev_code = await auth.request_login_otp(phone)
    return _render(request, "login.html", stage="otp", phone=phone, dev_code=dev_code)


@router.post("/login/verify")
async def login_verify(
    request: Request,
    db: DbDep,
    phone: Annotated[str, Form()],
    otp: Annotated[str, Form()],
) -> RedirectResponse:
    """Complete login: create a session and set the cookie."""
    settings = get_settings_from(request)
    auth = build_auth_service(db, get_redis_from(request), settings, request)
    login = await auth.complete_login(
        phone=phone,
        code=otp,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:255] or None,
    )
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        login.raw_session_token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        path="/admin",
        max_age=settings.ADMIN_SESSION_TTL_MINUTES * 60,
    )
    return response


@router.post("/logout")
async def logout(request: Request, db: DbDep) -> RedirectResponse:
    """Revoke the current session and clear the cookie."""
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        auth = build_auth_service(db, get_redis_from(request), get_settings_from(request), request)
        await auth.logout(raw)
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return response


# --- Step-up re-authentication ----------------------------------------------


@router.post("/step-up/request")
async def step_up_request(
    request: Request, db: DbDep, next_url: Annotated[str, Form(alias="next")]
) -> HTMLResponse:
    """Send a step-up OTP to the current admin's phone and show the entry form."""
    ctx = await _ctx(request, db)
    dev_code = await ctx.auth.request_login_otp(ctx.admin.phone)
    return _render(request, "step_up.html", ctx=ctx, next_url=next_url, dev_code=dev_code)


@router.post("/step-up")
async def step_up_verify(
    request: Request,
    db: DbDep,
    otp: Annotated[str, Form()],
    next_url: Annotated[str, Form(alias="next")],
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse:
    """Verify the step-up OTP and grant a short-lived step-up window."""
    ctx = await _ctx(request, db)
    verify_csrf(ctx, csrf_token, get_settings_from(request))
    await ctx.auth.grant_step_up(
        phone=ctx.admin.phone, code=otp, session_token_hash=ctx.session_row.session_token_hash
    )
    target = next_url if next_url.startswith("/admin") else "/admin"
    return RedirectResponse(target, status_code=303)


# --- Overview ----------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def overview(request: Request, db: DbDep) -> HTMLResponse:
    """Show the dashboard overview."""
    ctx = await _ctx(request, db)
    data = await ctx.service.overview()
    return _render(request, "overview.html", ctx=ctx, overview=data)


# --- Couriers ----------------------------------------------------------------


@router.get("/couriers", response_class=HTMLResponse)
async def couriers(request: Request, db: DbDep) -> HTMLResponse:
    """List couriers pending verification."""
    ctx = await _ctx(request, db)
    pending = await ctx.service.list_pending_couriers()
    return _render(request, "couriers.html", ctx=ctx, pending=pending)


@router.get("/couriers/{courier_id}", response_class=HTMLResponse)
async def courier_detail(request: Request, db: DbDep, courier_id: uuid.UUID) -> HTMLResponse:
    """Show a courier profile with identity numbers masked by default."""
    ctx = await _ctx(request, db)
    profile = await ctx.service.get_courier(courier_id)
    user = await ctx.service.get_user(courier_id)
    return _render(
        request, "courier_detail.html", ctx=ctx, profile=profile, user=user, revealed=None
    )


@router.post("/couriers/{courier_id}/verify")
async def courier_verify(
    request: Request,
    db: DbDep,
    courier_id: uuid.UUID,
    decision: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Approve or reject a courier's verification."""
    ctx = await _ctx(request, db)
    verify_csrf(ctx, csrf_token, get_settings_from(request))
    await require_step_up(ctx)
    await ctx.service.verify_courier(
        admin_id=ctx.admin.id,
        courier_user_id=courier_id,
        approve=decision == "approve",
        note=note or None,
        ip=client_ip(request),
    )
    return RedirectResponse(f"/admin/couriers/{courier_id}", status_code=303)


@router.post("/couriers/{courier_id}/reveal-identity", response_class=HTMLResponse)
async def courier_reveal(
    request: Request, db: DbDep, courier_id: uuid.UUID, csrf_token: Annotated[str, Form()]
) -> HTMLResponse:
    """Reveal a courier's identity documents once (step-up + audit)."""
    ctx = await _ctx(request, db)
    verify_csrf(ctx, csrf_token, get_settings_from(request))
    await require_step_up(ctx)
    revealed = await ctx.service.reveal_identity(
        admin_id=ctx.admin.id, courier_user_id=courier_id, ip=client_ip(request)
    )
    profile = await ctx.service.get_courier(courier_id)
    user = await ctx.service.get_user(courier_id)
    response = _render(
        request, "courier_detail.html", ctx=ctx, profile=profile, user=user, revealed=revealed
    )
    response.headers["Cache-Control"] = "no-store"
    return response


# --- Orders / invoices (read-only) ------------------------------------------


@router.get("/orders", response_class=HTMLResponse)
async def orders(request: Request, db: DbDep) -> HTMLResponse:
    """List recent orders."""
    ctx = await _ctx(request, db)
    rows = await ctx.service.list_orders()
    return _render(request, "orders.html", ctx=ctx, orders=rows)


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(request: Request, db: DbDep, order_id: uuid.UUID) -> HTMLResponse:
    """Show an order."""
    ctx = await _ctx(request, db)
    order = await ctx.service.get_order(order_id)
    return _render(request, "order_detail.html", ctx=ctx, order=order)


@router.get("/invoices", response_class=HTMLResponse)
async def invoices(request: Request, db: DbDep) -> HTMLResponse:
    """List recent invoices (read-only; admins never author invoices)."""
    ctx = await _ctx(request, db)
    rows = await ctx.service.list_invoices()
    return _render(request, "invoices.html", ctx=ctx, invoices=rows)


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
async def invoice_detail(request: Request, db: DbDep, invoice_id: uuid.UUID) -> HTMLResponse:
    """Show an invoice."""
    ctx = await _ctx(request, db)
    invoice = await ctx.service.get_invoice(invoice_id)
    return _render(request, "invoice_detail.html", ctx=ctx, invoice=invoice)


# --- Promos ------------------------------------------------------------------


@router.get("/promos", response_class=HTMLResponse)
async def promos(request: Request, db: DbDep) -> HTMLResponse:
    """List promos."""
    ctx = await _ctx(request, db)
    rows = await ctx.service.list_promos()
    return _render(request, "promos.html", ctx=ctx, promos=rows)


@router.get("/promos/new", response_class=HTMLResponse)
async def promo_new(request: Request, db: DbDep) -> HTMLResponse:
    """Show the promo creation form."""
    ctx = await _ctx(request, db)
    return _render(request, "promo_new.html", ctx=ctx)


@router.post("/promos")
async def promo_create(
    request: Request,
    db: DbDep,
    csrf_token: Annotated[str, Form()],
    code: Annotated[str, Form()],
    description: Annotated[str, Form()],
    discount_type: Annotated[str, Form()],
    min_order_amount: Annotated[str, Form()] = "0.00",
    max_usages_per_user: Annotated[int, Form()] = 1,
    percent_value: Annotated[str, Form()] = "",
    fixed_amount: Annotated[str, Form()] = "",
    max_discount_amount: Annotated[str, Form()] = "",
    max_total_usages: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Create a promo (step-up + CSRF + audit)."""
    ctx = await _ctx(request, db)
    verify_csrf(ctx, csrf_token, get_settings_from(request))
    await require_step_up(ctx)
    promo_id = await ctx.service.create_promo(
        admin_id=ctx.admin.id,
        code=code,
        description=description,
        discount_type=PromoDiscountType(discount_type),
        percent_value=Decimal(percent_value) if percent_value else None,
        fixed_amount=Decimal(fixed_amount) if fixed_amount else None,
        max_discount_amount=Decimal(max_discount_amount) if max_discount_amount else None,
        min_order_amount=Decimal(min_order_amount or "0.00"),
        max_total_usages=int(max_total_usages) if max_total_usages else None,
        max_usages_per_user=max_usages_per_user,
        ip=client_ip(request),
    )
    return RedirectResponse(f"/admin/promos/{promo_id}", status_code=303)


@router.get("/promos/{promo_id}", response_class=HTMLResponse)
async def promo_detail(request: Request, db: DbDep, promo_id: uuid.UUID) -> HTMLResponse:
    """Show a promo."""
    ctx = await _ctx(request, db)
    promo = await ctx.service.get_promo(promo_id)
    return _render(request, "promo_detail.html", ctx=ctx, promo=promo)


@router.post("/promos/{promo_id}/activate")
async def promo_activate(
    request: Request, db: DbDep, promo_id: uuid.UUID, csrf_token: Annotated[str, Form()]
) -> RedirectResponse:
    """Activate a promo (step-up + CSRF + audit)."""
    ctx = await _ctx(request, db)
    verify_csrf(ctx, csrf_token, get_settings_from(request))
    await require_step_up(ctx)
    await ctx.service.set_promo_active(
        admin_id=ctx.admin.id, promo_id=promo_id, active=True, ip=client_ip(request)
    )
    return RedirectResponse(f"/admin/promos/{promo_id}", status_code=303)


@router.post("/promos/{promo_id}/deactivate")
async def promo_deactivate(
    request: Request, db: DbDep, promo_id: uuid.UUID, csrf_token: Annotated[str, Form()]
) -> RedirectResponse:
    """Deactivate a promo (step-up + CSRF + audit)."""
    ctx = await _ctx(request, db)
    verify_csrf(ctx, csrf_token, get_settings_from(request))
    await require_step_up(ctx)
    await ctx.service.set_promo_active(
        admin_id=ctx.admin.id, promo_id=promo_id, active=False, ip=client_ip(request)
    )
    return RedirectResponse(f"/admin/promos/{promo_id}", status_code=303)


@router.get("/promos/{promo_id}/redemptions", response_class=HTMLResponse)
async def promo_redemptions(request: Request, db: DbDep, promo_id: uuid.UUID) -> HTMLResponse:
    """List a promo's redemptions."""
    ctx = await _ctx(request, db)
    rows = await ctx.service.list_promo_redemptions(promo_id)
    return _render(request, "promo_redemptions.html", ctx=ctx, redemptions=rows, promo_id=promo_id)


# --- Disputes / withdrawals / wallets / topups (read-only) ------------------


@router.get("/disputes", response_class=HTMLResponse)
async def disputes(request: Request, db: DbDep) -> HTMLResponse:
    """List disputes."""
    ctx = await _ctx(request, db)
    rows = await ctx.service.list_disputes()
    return _render(request, "disputes.html", ctx=ctx, disputes=rows)


@router.get("/disputes/{dispute_id}", response_class=HTMLResponse)
async def dispute_detail(request: Request, db: DbDep, dispute_id: uuid.UUID) -> HTMLResponse:
    """Show a dispute (resolution moves money and belongs to the ledger service)."""
    ctx = await _ctx(request, db)
    dispute = await ctx.service.get_dispute(dispute_id)
    return _render(request, "dispute_detail.html", ctx=ctx, dispute=dispute)


@router.get("/withdrawals", response_class=HTMLResponse)
async def withdrawals(request: Request, db: DbDep) -> HTMLResponse:
    """List withdrawals (IBANs masked; processing moves money via the ledger)."""
    ctx = await _ctx(request, db)
    rows = await ctx.service.list_withdrawals()
    return _render(request, "withdrawals.html", ctx=ctx, withdrawals=rows)


@router.get("/wallets", response_class=HTMLResponse)
async def wallets(request: Request, db: DbDep) -> HTMLResponse:
    """List system and user wallets."""
    ctx = await _ctx(request, db)
    rows = await ctx.service.list_wallets()
    return _render(request, "wallets.html", ctx=ctx, wallets=rows)


@router.get("/wallets/{wallet_id}", response_class=HTMLResponse)
async def wallet_detail(request: Request, db: DbDep, wallet_id: uuid.UUID) -> HTMLResponse:
    """Show a wallet."""
    ctx = await _ctx(request, db)
    wallet = await ctx.service.get_wallet(wallet_id)
    return _render(request, "wallet_detail.html", ctx=ctx, wallet=wallet)


@router.get("/topups", response_class=HTMLResponse)
async def topups(request: Request, db: DbDep) -> HTMLResponse:
    """List wallet top-up intents."""
    ctx = await _ctx(request, db)
    rows = await ctx.service.list_topups()
    return _render(request, "topups.html", ctx=ctx, topups=rows)


# --- Users -------------------------------------------------------------------


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(request: Request, db: DbDep, user_id: uuid.UUID) -> HTMLResponse:
    """Show a user."""
    ctx = await _ctx(request, db)
    user = await ctx.service.get_user(user_id)
    return _render(request, "user_detail.html", ctx=ctx, user=user)


@router.post("/users/{user_id}/ban")
async def user_ban(
    request: Request, db: DbDep, user_id: uuid.UUID, csrf_token: Annotated[str, Form()]
) -> RedirectResponse:
    """Ban a user (step-up + CSRF + audit)."""
    ctx = await _ctx(request, db)
    verify_csrf(ctx, csrf_token, get_settings_from(request))
    await require_step_up(ctx)
    await ctx.service.set_user_banned(
        admin_id=ctx.admin.id, user_id=user_id, banned=True, ip=client_ip(request)
    )
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/unban")
async def user_unban(
    request: Request, db: DbDep, user_id: uuid.UUID, csrf_token: Annotated[str, Form()]
) -> RedirectResponse:
    """Unban a user (step-up + CSRF + audit)."""
    ctx = await _ctx(request, db)
    verify_csrf(ctx, csrf_token, get_settings_from(request))
    await require_step_up(ctx)
    await ctx.service.set_user_banned(
        admin_id=ctx.admin.id, user_id=user_id, banned=False, ip=client_ip(request)
    )
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


# --- Audit logs --------------------------------------------------------------


@router.get("/audit-logs", response_class=HTMLResponse)
async def audit_logs(request: Request, db: DbDep) -> HTMLResponse:
    """List recent audit-log entries."""
    ctx = await _ctx(request, db)
    rows = await ctx.service.list_audit_logs(limit=100)
    return _render(request, "audit_logs.html", ctx=ctx, logs=rows)
