"""Webhook delivery service with signature verification."""
import hmac
import hashlib
import json
from datetime import datetime
from typing import Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.webhook import WebhookEndpoint, WebhookDelivery
from app.models.payment import Payment, PaymentStatus
from app.core.config import get_settings

settings = get_settings()


class WebhookService:
    """Manages webhook endpoints and deliveries."""

    @staticmethod
    def generate_signature(payload: dict, secret: str) -> str:
        """Generate HMAC-SHA256 signature for webhook payload."""
        timestamp = str(int(datetime.utcnow().timestamp()))
        payload_str = json.dumps(payload, separators=(",", ":"), default=str)
        signed_payload = f"{timestamp}.{payload_str}"
        signature = hmac.new(
            secret.encode(),
            signed_payload.encode(),
            hashlib.sha256
        ).hexdigest()
        return f"t={timestamp},v1={signature}"

    @staticmethod
    def verify_signature(payload: dict, signature_header: str, secret: str) -> bool:
        """Verify webhook signature."""
        try:
            parts = dict(p.split("=") for p in signature_header.split(","))
            timestamp = parts.get("t", "")
            received_sig = parts.get("v1", "")

            payload_str = json.dumps(payload, separators=(",", ":"), default=str)
            signed_payload = f"{timestamp}.{payload_str}"
            expected_sig = hmac.new(
                secret.encode(),
                signed_payload.encode(),
                hashlib.sha256
            ).hexdigest()

            return hmac.compare_digest(received_sig, expected_sig)
        except Exception:
            return False

    async def create_endpoint(
        self,
        db: AsyncSession,
        merchant_id: UUID,
        url: str,
        events: list[str],
        secret: Optional[str] = None
    ) -> WebhookEndpoint:
        """Create a new webhook endpoint."""
        import secrets

        endpoint = WebhookEndpoint(
            merchant_id=merchant_id,
            url=url,
            secret=secret or secrets.token_urlsafe(32),
            events=events,
        )
        db.add(endpoint)
        await db.flush()
        return endpoint

    async def get_endpoints_for_event(
        self,
        db: AsyncSession,
        merchant_id: UUID,
        event_type: str
    ) -> list[WebhookEndpoint]:
        """Get active endpoints subscribed to an event."""
        result = await db.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.merchant_id == merchant_id,
                WebhookEndpoint.is_active == True,
            )
        )
        endpoints = result.scalars().all()

        # Filter by event subscription
        return [
            ep for ep in endpoints
            if not ep.events or event_type in ep.events or "*" in ep.events
        ]

    async def queue_delivery(
        self,
        db: AsyncSession,
        endpoint: WebhookEndpoint,
        event_type: str,
        payload: dict
    ) -> WebhookDelivery:
        """Queue a webhook for delivery."""
        delivery = WebhookDelivery(
            endpoint_id=endpoint.id,
            event_type=event_type,
            payload=payload,
            status="pending",
            attempt_count=0,
        )
        db.add(delivery)
        await db.flush()
        return delivery

    def build_payment_payload(self, payment: Payment, event_type: str) -> dict:
        """Build standardized webhook payload for payment events."""
        return {
            "id": str(payment.id),
            "event": event_type,
            "data": {
                "id": str(payment.id),
                "status": payment.status,
                "amount_cents": payment.amount_cents,
                "currency": payment.currency,
                "fee_cents": payment.fee_cents,
                "net_amount_cents": payment.net_amount_cents,
                "payment_method": payment.payment_method,
                "provider": payment.provider,
                "provider_reference": payment.provider_reference,
                "merchant_reference": payment.merchant_reference,
                "customer_email": payment.customer_email,
                "description": payment.description,
                "created_at": payment.created_at.isoformat() if payment.created_at else None,
                "captured_at": payment.captured_at.isoformat() if payment.captured_at else None,
                "settled_at": payment.settled_at.isoformat() if payment.settled_at else None,
            },
            "created_at": datetime.utcnow().isoformat(),
        }
