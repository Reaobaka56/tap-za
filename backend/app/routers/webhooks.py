"""Webhook endpoint management."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.deps import get_current_merchant
from app.models.merchant import Merchant
from app.models.webhook import WebhookEndpoint
from app.schemas import WebhookCreate, WebhookCreated, WebhookOut
from app.services.webhook import SUPPORTED_EVENTS, WebhookService, validate_webhook_url

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post("", response_model=WebhookCreated, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    body: WebhookCreate, merchant: Merchant = Depends(get_current_merchant), db: AsyncSession = Depends(get_db)
):
    """Register an endpoint. The signing secret is returned once."""
    try:
        validate_webhook_url(body.url)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    unknown = [e for e in body.events if e not in SUPPORTED_EVENTS]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown events: {unknown}. Supported: {SUPPORTED_EVENTS}",
        )
    endpoint = await WebhookService().create_endpoint(db, merchant.id, body.url, body.events)
    await db.commit()
    return WebhookCreated(
        id=endpoint.id, url=endpoint.url, events=endpoint.events, is_active=endpoint.is_active,
        created_at=endpoint.created_at, secret=endpoint.secret,
    )


@router.get("", response_model=list[WebhookOut])  # never includes the secret
async def list_webhooks(merchant: Merchant = Depends(get_current_merchant), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(WebhookEndpoint).where(WebhookEndpoint.merchant_id == merchant.id).order_by(WebhookEndpoint.created_at)
    )
    return result.scalars().all()


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: UUID, merchant: Merchant = Depends(get_current_merchant), db: AsyncSession = Depends(get_db)
):
    endpoint = await db.get(WebhookEndpoint, webhook_id)
    if not endpoint or endpoint.merchant_id != merchant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
    endpoint.is_active = False
    await db.commit()
