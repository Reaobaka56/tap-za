"""Payment creation, lookup, hosted checkout, provider callback and refunds."""
import html
import json
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, get_db
from app.core.deps import get_current_merchant
from app.models.merchant import Merchant
from app.models.payment import Payment, PaymentStatus
from app.schemas import PaymentCreate, PaymentCreateResponse, PaymentOut
from app.services.orchestrator import IdempotencyConflict, PaymentOrchestrator
from app.services.payment_engine import PaymentEngine
from app.services.webhook import WebhookService

router = APIRouter(prefix="/v1/payments", tags=["payments"])
settings = get_settings()
orchestrator = PaymentOrchestrator()
engine = PaymentEngine()
webhooks = WebhookService()


async def _deliver(delivery_ids: list[UUID]) -> None:
    """Background task: attempt first delivery for each queued webhook."""
    for delivery_id in delivery_ids:
        async with AsyncSessionLocal() as session:
            await webhooks.deliver(session, delivery_id)


def _schedule(background: BackgroundTasks, ids: list[UUID]) -> None:
    if ids:
        background.add_task(_deliver, ids)


async def _own_payment(db: AsyncSession, merchant: Merchant, payment_id: UUID, for_update=False) -> Payment:
    payment = await engine.get_payment(db, payment_id, merchant.id, for_update=for_update)
    if not payment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    return payment


@router.post("", response_model=PaymentCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_payment(
    body: PaymentCreate,
    response: Response,
    background: BackgroundTasks,
    idempotency_key: Optional[str] = Header(default=None, max_length=100),
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
):
    fee, net = engine.calculate_fees(body.amount)
    if net <= 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Amount too small: gateway fee is {fee} cents"
        )
    try:
        result = await orchestrator.create_and_route_payment(
            db=db,
            merchant_id=merchant.id,
            amount_cents=body.amount,
            currency=body.currency,
            methods=body.methods,
            merchant_reference=body.merchant_reference,
            customer_email=body.customer_email,
            customer_name=body.customer_name,
            return_url=body.return_url,
            cancel_url=body.cancel_url,
            description=body.description,
            idempotency_key=idempotency_key,
            metadata=body.metadata,
        )
    except IdempotencyConflict as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    payment: Payment = result["payment"]
    if result["created"]:
        event = "payment.created" if payment.status == PaymentStatus.PENDING.value else "payment.failed"
        ids = await webhooks.dispatch_event(db, payment, event)
        await db.commit()
        _schedule(background, ids)
    else:
        response.status_code = status.HTTP_200_OK  # idempotent replay

    out = PaymentCreateResponse(
        **PaymentOut.from_payment(payment).model_dump(),
        requires_redirect=result.get("requires_redirect", False),
        error=result.get("error"),
    )
    if result.get("error") and payment.status == PaymentStatus.FAILED.value:
        return JSONResponse(status_code=status.HTTP_402_PAYMENT_REQUIRED, content=jsonable_encoder(out))
    return out


@router.get("", response_model=list[PaymentOut])
async def list_payments(
    status_filter: Optional[PaymentStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
):
    payments = await engine.list_payments(db, merchant.id, status_filter, limit, offset)
    return [PaymentOut.from_payment(p) for p in payments]


@router.get("/{payment_id}", response_model=PaymentOut)
async def get_payment(
    payment_id: UUID, merchant: Merchant = Depends(get_current_merchant), db: AsyncSession = Depends(get_db)
):
    return PaymentOut.from_payment(await _own_payment(db, merchant, payment_id))


@router.get("/{payment_id}/ledger")
async def payment_ledger(
    payment_id: UUID, merchant: Merchant = Depends(get_current_merchant), db: AsyncSession = Depends(get_db)
):
    from app.services.ledger import LedgerService
    await _own_payment(db, merchant, payment_id)
    entries = await LedgerService().get_ledger_for_payment(db, payment_id)
    return [
        {
            "id": str(e.id),
            "entry_type": e.entry_type.value,
            "amount_cents": e.amount_cents,
            "debit_account_id": str(e.debit_account_id),
            "credit_account_id": str(e.credit_account_id),
            "description": e.description,
            "created_at": e.created_at,
        }
        for e in entries
    ]


@router.post("/{payment_id}/refund", response_model=PaymentOut)
async def refund_payment(
    payment_id: UUID,
    background: BackgroundTasks,
    amount: Optional[int] = Query(default=None, gt=0, description="Cents; omit for a full refund"),
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
):
    payment = await _own_payment(db, merchant, payment_id, for_update=True)
    try:
        await orchestrator.process_refund(db, payment, amount)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    ids = await webhooks.dispatch_event(db, payment, "payment.refunded")
    await db.commit()
    _schedule(background, ids)
    return PaymentOut.from_payment(payment)


# --- Sandbox hosted checkout and provider callback -------------------------------------------------
# These stand in for a real provider's checkout page and server-to-server callback. They are
# disabled in production: a real callback must be authenticated with the provider's signature.

def _sandbox_only() -> None:
    if settings.ENVIRONMENT == "production":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Sandbox checkout/callback is disabled in production; provider signature verification is required",
        )


@router.post("/{payment_id}/callback", response_model=PaymentOut)
async def provider_callback(
    payment_id: UUID,
    background: BackgroundTasks,
    success: bool = True,
    cancelled: bool = False,
    db: AsyncSession = Depends(get_db),
):
    _sandbox_only()
    payment = await engine.get_payment(db, payment_id, for_update=True)
    if not payment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    before = payment.status
    ids: list[UUID] = []
    try:
        if cancelled:
            await orchestrator.cancel_payment(db, payment)
        else:
            await orchestrator.process_callback(db, payment, success)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    if payment.status != before:
        if payment.status == PaymentStatus.SETTLED.value:
            ids += await webhooks.dispatch_event(db, payment, "payment.captured", status="captured")
            ids += await webhooks.dispatch_event(db, payment, "payment.settled")
        elif payment.status == PaymentStatus.FAILED.value:
            ids += await webhooks.dispatch_event(db, payment, "payment.failed")
        elif payment.status == PaymentStatus.CANCELLED.value:
            ids += await webhooks.dispatch_event(db, payment, "payment.cancelled")
    await db.commit()
    _schedule(background, ids)
    return PaymentOut.from_payment(payment)


_CHECKOUT_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Checkout</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0b0b0c;color:#f2f2f2;display:grid;place-items:center;min-height:100vh;margin:0}}
.card{{background:#161618;border:1px solid #2a2a2e;border-radius:14px;padding:32px;width:min(380px,92vw)}}
h1{{font-size:14px;font-weight:500;color:#9a9aa2;margin:0 0 4px}}.amt{{font-size:34px;font-weight:650;margin:0 0 6px}}
p{{color:#9a9aa2;font-size:14px;margin:0 0 22px}}.tag{{display:inline-block;font-size:11px;background:#2a2a2e;border-radius:6px;padding:3px 8px;margin-bottom:18px;color:#d6a84a}}
button{{width:100%;padding:13px;border-radius:9px;border:0;font-size:15px;font-weight:600;cursor:pointer;margin-bottom:10px}}
.pay{{background:#f2f2f2;color:#0b0b0c}}.no{{background:transparent;color:#9a9aa2;border:1px solid #2a2a2e}}#msg{{font-size:14px;margin-top:6px}}
</style></head><body><div class="card">
<div class="tag">SANDBOX - no real money moves</div>
<h1>{merchant_ref}</h1><div class="amt">{amount}</div><p>{description}</p>
{body}
<div id="msg"></div></div>
<script>
const PAYMENT_ID = {payment_id_js};
const RETURN_URL = {return_url_js};
const CANCEL_URL = {cancel_url_js};
async function act(q, next) {{
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  const r = await fetch('/v1/payments/' + PAYMENT_ID + '/callback?' + q, {{method: 'POST'}});
  const msg = document.getElementById('msg');
  if (!r.ok) {{ msg.textContent = 'Something went wrong (' + r.status + ').'; return; }}
  const p = await r.json();
  msg.textContent = 'Payment ' + p.status + '.';
  if (next) setTimeout(() => {{ location.href = next; }}, 900);
}}
document.getElementById('pay')?.addEventListener('click', () => act('success=true', RETURN_URL));
document.getElementById('decline')?.addEventListener('click', () => act('success=false', CANCEL_URL));
document.getElementById('cancel')?.addEventListener('click', () => act('cancelled=true', CANCEL_URL));
</script></body></html>"""


def _js(value) -> str:
    """Embed a value in an inline <script> safely."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


@router.get("/{payment_id}/checkout", response_class=HTMLResponse)
async def hosted_checkout(payment_id: UUID, db: AsyncSession = Depends(get_db)):
    _sandbox_only()
    payment = await engine.get_payment(db, payment_id)
    if not payment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    if payment.status == PaymentStatus.PENDING.value:
        body = (
            '<button class="pay" id="pay">Pay now</button>'
            '<button class="no" id="decline">Simulate decline</button>'
            '<button class="no" id="cancel">Cancel</button>'
        )
    else:
        body = f'<p>This payment is {html.escape(payment.status)}.</p>'
    page = _CHECKOUT_PAGE.format(
        merchant_ref=html.escape(payment.merchant_reference or "Payment"),
        amount=html.escape(payment.amount_display),
        description=html.escape(payment.description or ""),
        body=body,
        payment_id_js=_js(str(payment.id)),
        return_url_js=_js(payment.return_url),
        cancel_url_js=_js(payment.cancel_url or payment.return_url),
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})
