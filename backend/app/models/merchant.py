"""Merchant and API key models."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Text, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base


class Merchant(Base):
    __tablename__ = "merchants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    business_name = Column(String(255), nullable=False)
    business_registration = Column(String(100), nullable=True)
    status = Column(String(20), default="active")  # active, suspended, pending_kyc
    environment = Column(String(10), default="test")  # test, live
    webhook_url = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    api_keys = relationship("APIKey", back_populates="merchant", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="merchant")
    accounts = relationship("Account", back_populates="merchant")
    webhook_endpoints = relationship("WebhookEndpoint", back_populates="merchant")


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id = Column(UUID(as_uuid=True), ForeignKey("merchants.id"), nullable=False)
    key_prefix = Column(String(20), nullable=False)
    key_hash = Column(String(255), nullable=False)
    name = Column(String(100), nullable=False)
    environment = Column(String(10), default="test")  # test, live
    permissions = Column(String(255), default="read,write")  # comma-separated
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    revoked_at = Column(DateTime, nullable=True)

    merchant = relationship("Merchant", back_populates="api_keys")

    __table_args__ = (
        Index("idx_api_keys_merchant_env", "merchant_id", "environment"),
    )
