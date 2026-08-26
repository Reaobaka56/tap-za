"""Webhook endpoint and delivery tracking."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, Text, ForeignKey, JSON, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id = Column(UUID(as_uuid=True), ForeignKey("merchants.id"), nullable=False)
    url = Column(Text, nullable=False)
    secret = Column(String(255), nullable=False)
    events = Column(JSON, default=list)  # ["payment.created", "payment.captured", etc.]
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    merchant = relationship("Merchant", back_populates="webhook_endpoints")
    deliveries = relationship("WebhookDelivery", back_populates="endpoint")


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    endpoint_id = Column(UUID(as_uuid=True), ForeignKey("webhook_endpoints.id"), nullable=False)
    event_type = Column(String(50), nullable=False)
    payload = Column(JSON, nullable=False)

    # Delivery tracking
    status = Column(String(20), default="pending")  # pending, delivered, failed
    http_status = Column(Integer, nullable=True)
    response_body = Column(Text, nullable=True)
    attempt_count = Column(Integer, default=0)
    next_retry_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    delivered_at = Column(DateTime, nullable=True)

    endpoint = relationship("WebhookEndpoint", back_populates="deliveries")
