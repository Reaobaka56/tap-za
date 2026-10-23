"""Webhook delivery service with signature verification."""
import hmac
import hashlib
import ipaddress
import json
import socket
from datetime import datetime, timedelta
from urllib.parse import urlparse
from typing import Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.webhook import WebhookEndpoint, WebhookDelivery
from app.models.payment import Payment, PaymentStatus
from app.core.config import get_settings

settings = get_settings()

EVENT_FOR_STATUS = {
    PaymentStatus.PENDING.value: "payment.created",
    PaymentStatus.AUTHORIZED.value: "payment.authorized",
    PaymentStatus.CAPTURED.value: "payment.captured",
    PaymentStatus.SETTLED.value: "payment.settled",
    PaymentStatus.FAILED.value: "payment.failed",
    PaymentStatus.CANCELLED.value: "payment.cancelled",
    PaymentStatus.REFUNDED.value: "payment.refunded",
    PaymentStatus.PARTIALLY_REFUNDED.value: "payment.refunded",
}
SUPPORTED_EVENTS = sorted(set(EVENT_FOR_STATUS.values())) + ["*"]


def validate_webhook_url(url: str) -> None:
    """Reject webhook targets that could be used for SSRF.

    Outside production, http and localhost are allowed for local development.
    In production the URL must be https and must not resolve to a private,
    loopback, link-local or otherwise non-public address.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Webhook URL must be an absolute http(s) URL")
    if settings.ENVIRONMENT != "production":
        return
    if parsed.scheme != "https":
        raise ValueError("Webhook URL must use https in production")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError("Webhook host could not be resolved")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("Webhook URL must not point to a private or internal address")


class WebhookService:
    """Manages webhook endpoints and deliveries."""

    @staticmethod
    def serialize(payload: dict) -> str:
        return json.dumps(payload, separators=(",", ":"), default=str)

    @staticmethod
    def generate_signature(payload: dict, secret: str, timestamp: Optional[str] = None) -> str:
        """Generate HMAC-SHA256 signature for webhook payload."""
        timestamp = timestamp or str(int(datetime.utcnow().timestamp()))
        payload_str = json.dumps(payload, separators=(",", ":"), default=str)
        signed_payload = f"{timestamp}.{payload_str}"
        signature = hmac.new(
            secret.encode(),
            signed_payload.encode(),
            hashlib.sha256
        ).hexdigest()
        return f"t={timestamp},v1={signature}"

    @staticmethod
    def verify_signature(
        payload: dict, signature_header: str, secret: str, tolerance_seconds: Optional[int] = 300
    ) -> bool:
        """Verify webhook signature (and, by default, reject timestamps older than 5 minutes)."""
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

            if tolerance_seconds is not None:
                if abs(datetime.utcnow().timestamp() - int(timestamp)) > tolerance_seconds:
                    return False
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
            next_retry_at=datetime.utcnow(),  # so the retry loop also picks up undelivered first attempts
        )
        db.add(delivery)
        await db.flush()
        return delivery

    def build_payment_payload(self, payment: Payment, event_type: str, status: Optional[str] = None) -> dict:
        """Build standardized webhook payload for payment events."""
        return {
            "id": str(payment.id),
            "event": event_type,
            "data": {
                "id": str(payment.id),
                "status": status or payment.status,
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

    async def dispatch_event(
        self, db: AsyncSession, payment: Payment, event_type: str, status: Optional[str] = None
    ) -> list[UUID]:
        """Queue a delivery for every subscribed endpoint. Returns the delivery ids."""
        endpoints = await self.get_endpoints_for_event(db, payment.merchant_id, event_type)
        payload = self.build_payment_payload(payment, event_type, status)
        ids = []
        for ep in endpoints:
            delivery = await self.queue_delivery(db, ep, event_type, payload)
            ids.append(delivery.id)
        return ids

    async def deliver(self, db: AsyncSession, delivery_id: UUID, client=None) -> Optional[WebhookDelivery]:
        """Attempt one delivery. Updates attempt bookkeeping and schedules a retry on failure."""
        import httpx

        result = await db.execute(
            select(WebhookDelivery).where(WebhookDelivery.id == delivery_id).with_for_update()
        )
        delivery = result.scalar_one_or_none()
        if not delivery or delivery.status == "delivered":
            return delivery
        endpoint = await db.get(WebhookEndpoint, delivery.endpoint_id)
        if not endpoint or not endpoint.is_active:
            delivery.status = "failed"
            await db.commit()
            return delivery

        body = self.serialize(delivery.payload)
        timestamp = str(int(datetime.utcnow().timestamp()))
        signed = hmac.new(endpoint.secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": f"t={timestamp},v1={signed}",
            "X-Webhook-Event": delivery.event_type,
        }

        delivery.attempt_count += 1
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=settings.WEBHOOK_TIMEOUT_SECONDS, follow_redirects=False)
        try:
            validate_webhook_url(endpoint.url)
            resp = await client.post(endpoint.url, content=body, headers=headers)
            delivery.http_status = resp.status_code
            delivery.response_body = resp.text[:1000]
            ok = 200 <= resp.status_code < 300
        except Exception as exc:  # network error, timeout, blocked URL
            delivery.http_status = None
            delivery.response_body = f"{type(exc).__name__}: {exc}"[:1000]
            ok = False
        finally:
            if owns_client:
                await client.aclose()

        if ok:
            delivery.status = "delivered"
            delivery.delivered_at = datetime.utcnow()
            delivery.next_retry_at = None
        elif delivery.attempt_count >= settings.WEBHOOK_MAX_ATTEMPTS:
            delivery.status = "failed"
            delivery.next_retry_at = None
        else:
            delivery.status = "pending"
            delivery.next_retry_at = datetime.utcnow() + timedelta(seconds=30 * 2 ** (delivery.attempt_count - 1))
        await db.commit()
        return delivery

    async def due_delivery_ids(self, db: AsyncSession, limit: int = 50) -> list[UUID]:
        result = await db.execute(
            select(WebhookDelivery.id).where(
                WebhookDelivery.status == "pending",
                WebhookDelivery.next_retry_at.is_not(None),
                WebhookDelivery.next_retry_at <= datetime.utcnow(),
            ).limit(limit)
        )
        return list(result.scalars().all())
