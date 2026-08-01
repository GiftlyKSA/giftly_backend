"""Admin dashboard routes (SPEC SECTION 18.3).

Server-rendered Jinja pages mounted at ``/admin``. Every route calls the admin
services and never queries the DB directly. Reads are open to any authenticated
admin; every mutating action requires CSRF, writes an audit row, and — for revealing
Restricted data — a fresh step-up grant. Money-moving resolutions (disputes,
withdrawals) are shown read-only; they belong to the ledger service.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request, Response
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
from app.core.exceptions import RateLimitedError, UnauthorizedError

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DbDep = Annotated[AsyncSession, Depends(get_db)]


def _render(
    request: Request, template: str, *, status_code: int = 200, **context: object
) -> HTMLResponse:
    """Render a template with the request bound."""
    return _TEMPLATES.TemplateResponse(
        request, template, {"request": request, **context}, status_code=status_code
    )


async def _ctx(request: Request, db: AsyncSession) -> AdminContext:
    return await require_admin(request, db)


# --- Authentication ----------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Show the admin username/password login form."""
    return _render(request, "login.html")


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    db: DbDep,
    username: Annotated[str, Form(min_length=1, max_length=128)],
    password: Annotated[str, Form(min_length=1, max_length=1024)],
) -> Response:
    """Verify environment credentials, create a session, and set its cookie."""
    settings = get_settings_from(request)
    auth = build_auth_service(db, get_redis_from(request), settings)
    try:
        result = await auth.complete_login(
            username=username,
            password=password,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:255] or None,
        )
    except (UnauthorizedError, RateLimitedError) as exc:
        return _render(
            request,
            "login.html",
            status_code=exc.status_code,
            username=username,
            error=exc.message,
        )
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        result.raw_session_token,
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
        auth = build_auth_service(db, get_redis_from(request), get_settings_from(request))
        await auth.logout(raw)
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return response


# --- Step-up re-authentication ----------------------------------------------


@router.post("/step-up/request")
async def step_up_request(
    request: Request, db: DbDep, next_url: Annotated[str, Form(alias="next")]
) -> HTMLResponse:
    """Show password confirmation for a sensitive admin action."""
    ctx = await _ctx(request, db)
    return _render(request, "step_up.html", ctx=ctx, next_url=next_url)


@router.post("/step-up")
async def step_up_verify(
    request: Request,
    db: DbDep,
    password: Annotated[str, Form(min_length=1, max_length=1024)],
    next_url: Annotated[str, Form(alias="next")],
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse:
    """Verify the step-up OTP and grant a short-lived step-up window."""
    ctx = await _ctx(request, db)
    verify_csrf(ctx, csrf_token, get_settings_from(request))
    await ctx.auth.grant_step_up(
        password=password,
        session_token_hash=ctx.session_row.session_token_hash,
        ip=client_ip(request),
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


# --- Application data tables -------------------------------------------------


@router.get("/tables", response_class=HTMLResponse)
async def table_catalog(request: Request, db: DbDep) -> HTMLResponse:
    """List every application-owned table and its dashboard interaction mode."""
    ctx = await _ctx(request, db)
    tables = ctx.service.list_table_catalog()
    return _render(request, "tables.html", ctx=ctx, tables=tables, table_count=len(tables))


@router.get("/tables/{table_name}", response_class=HTMLResponse)
async def table_browser(
    request: Request,
    db: DbDep,
    table_name: str,
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
) -> HTMLResponse:
    """Render one bounded, redacted page from an application table."""
    ctx = await _ctx(request, db)
    data = await ctx.service.get_table_page(table_name, page=page)
    return _render(request, "table_browser.html", ctx=ctx, data=data)


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
        request,
        "courier_detail.html",
        ctx=ctx,
        profile=profile,
        user=user,
        revealed=None,
        can_edit=await ctx.auth.has_step_up(ctx.session_row.session_token_hash),
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
        request,
        "courier_detail.html",
        ctx=ctx,
        profile=profile,
        user=user,
        revealed=revealed,
        can_edit=True,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/couriers/{courier_id}/edit")
async def courier_edit(
    request: Request,
    db: DbDep,
    courier_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    city_of_residence: Annotated[str, Form(min_length=1, max_length=100)],
    bio: Annotated[str, Form(max_length=1000)] = "",
) -> RedirectResponse:
    """Update the safe public fields of a courier profile."""
    ctx = await _ctx(request, db)
    verify_csrf(ctx, csrf_token, get_settings_from(request))
    await require_step_up(ctx)
    await ctx.service.update_courier_profile(
        admin_id=ctx.admin.id,
        courier_user_id=courier_id,
        city_of_residence=city_of_residence.strip(),
        bio=bio.strip() or None,
        ip=client_ip(request),
    )
    return RedirectResponse(f"/admin/couriers/{courier_id}", status_code=303)


# --- Orders / invoices -------------------------------------------------------


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
    return _render(
        request,
        "order_detail.html",
        ctx=ctx,
        order=order,
        can_edit=await ctx.auth.has_step_up(ctx.session_row.session_token_hash),
    )


@router.post("/orders/{order_id}/edit")
async def order_edit(
    request: Request,
    db: DbDep,
    order_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    delivery_city: Annotated[str, Form(min_length=1, max_length=100)],
    delivery_date: Annotated[date, Form()],
    description: Annotated[str, Form(max_length=5000)] = "",
    delivery_address_note: Annotated[str, Form(max_length=255)] = "",
) -> RedirectResponse:
    """Update non-financial order details while the order is still editable."""
    ctx = await _ctx(request, db)
    verify_csrf(ctx, csrf_token, get_settings_from(request))
    await require_step_up(ctx)
    await ctx.service.update_order_details(
        admin_id=ctx.admin.id,
        order_id=order_id,
        description=description.strip() or None,
        delivery_city=delivery_city.strip(),
        delivery_date=delivery_date,
        delivery_address_note=delivery_address_note.strip() or None,
        ip=client_ip(request),
    )
    return RedirectResponse(f"/admin/orders/{order_id}", status_code=303)


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


# --- Promos (read-only) ------------------------------------------------------


@router.get("/promos", response_class=HTMLResponse)
async def promos(request: Request, db: DbDep) -> HTMLResponse:
    """List promos."""
    ctx = await _ctx(request, db)
    rows = await ctx.service.list_promos()
    return _render(request, "promos.html", ctx=ctx, promos=rows)


@router.get("/promos/{promo_id}", response_class=HTMLResponse)
async def promo_detail(request: Request, db: DbDep, promo_id: uuid.UUID) -> HTMLResponse:
    """Show a promo."""
    ctx = await _ctx(request, db)
    promo = await ctx.service.get_promo(promo_id)
    return _render(request, "promo_detail.html", ctx=ctx, promo=promo)


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
    return _render(
        request,
        "user_detail.html",
        ctx=ctx,
        user=user,
        can_edit=await ctx.auth.has_step_up(ctx.session_row.session_token_hash),
    )


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


@router.post("/users/{user_id}/edit")
async def user_edit(
    request: Request,
    db: DbDep,
    user_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    full_name: Annotated[str, Form(max_length=120)] = "",
    email: Annotated[str, Form(max_length=255)] = "",
) -> RedirectResponse:
    """Update a user's non-authentication profile fields."""
    ctx = await _ctx(request, db)
    verify_csrf(ctx, csrf_token, get_settings_from(request))
    await require_step_up(ctx)
    await ctx.service.update_user_profile(
        admin_id=ctx.admin.id,
        user_id=user_id,
        full_name=full_name.strip() or None,
        email=email.strip().lower() or None,
        ip=client_ip(request),
    )
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


# --- Audit logs --------------------------------------------------------------


@router.get("/audit-logs", response_class=HTMLResponse)
async def audit_logs(request: Request, db: DbDep) -> HTMLResponse:
    """List recent audit-log entries."""
    ctx = await _ctx(request, db)
    rows = await ctx.service.list_audit_logs(limit=100)
    return _render(request, "audit_logs.html", ctx=ctx, logs=rows)
