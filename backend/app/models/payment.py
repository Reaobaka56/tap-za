"""Payment model with state machine and idempotency."""
import uuid
from datetime import datetime
from enum import Enum as PyEnum
from sqlalchemy import Column, String, Integer, BigInteger, DateTime, Text, ForeignKey, Index, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base


class PaymentStatus(str, PyEnum):
    CREATED = "created"
    PENDING = "pending"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    SETTLED = "settled"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class PaymentMethod(str, PyEnum):
    CARD = "card"
    EFT = "eft"
    PAY_BY_BANK = "pay_by_bank"
    QR = "qr"
    WALLET = "wallet"


VALID_TRANSITIONS = {
    PaymentStatus.CREATED: [PaymentStatus.PENDING, PaymentStatus.FAILED, PaymentStatus.CANCELLED],
    PaymentStatus.PENDING: [PaymentStatus.AUTHORIZED, PaymentStatus.FAILED, PaymentStatus.EXPIRED, PaymentStatus.CANCELLED],
    PaymentStatus.AUTHORIZED: [PaymentStatus.CAPTURED, PaymentStatus.CANCELLED, PaymentStatus.FAILED],
    PaymentStatus.CAPTURED: [PaymentStatus.SETTLED, PaymentStatus.REFUND_PENDING],
    PaymentStatus.SETTLED: [PaymentStatus.REFUND_PENDING, PaymentStatus.REFUNDED, PaymentStatus.PARTIALLY_REFUNDED],
    PaymentStatus.REFUND_PENDING: [PaymentStatus.REFUNDED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.SETTLED],
    PaymentStatus.PARTIALLY_REFUNDED: [PaymentStatus.REFUND_PENDING, PaymentStatus.REFUNDED],
}


class Payment(Base):
    __tablename__ = "payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id = Column(UUID(as_uuid=True), ForeignKey("merchants.id"), nullable=False)

    # Idempotency
    idempotency_key = Column(String(100), nullable=True, index=True)

    # Amounts (stored in cents)
    amount_cents = Column(BigInteger, nullable=False)
    currency = Column(String(3), default="ZAR")
    fee_cents = Column(BigInteger, default=0)
    net_amount_cents = Column(BigInteger, default=0)
    refunded_amount_cents = Column(BigInteger, default=0)

    # Status and method
    status = Column(String(30), default=PaymentStatus.CREATED.value, index=True)
    payment_method = Column(String(30), nullable=True)
    provider = Column(String(50), nullable=True)  # e.g., "mock_card", "ozow", "payfast"
    provider_reference = Column(String(255), nullable=True)

    # Customer info
    customer_email = Column(String(255), nullable=True)
    customer_name = Column(String(255), nullable=True)
    customer_phone = Column(String(50), nullable=True)

    # Merchant reference
    merchant_reference = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)

    # URLs
    return_url = Column(Text, nullable=True)
    cancel_url = Column(Text, nullable=True)
    checkout_url = Column(Text, nullable=True)

    # Metadata
    metadata = Column(JSON, default=dict)

    # Risk
    risk_score = Column(Integer, nullable=True)
    risk_flags = Column(JSON, default=list)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    authorized_at = Column(DateTime, nullable=True)
    captured_at = Column(DateTime, nullable=True)
    settled_at = Column(DateTime, nullable=True)
    failed_at = Column(DateTime, nullable=True)

    # Relationships
    merchant = relationship("Merchant", back_populates="payments")
    ledger_entries = relationship("LedgerEntry", back_populates="payment")

    __table_args__ = (
        Index("idx_payments_merchant_status", "merchant_id", "status"),
        Index("idx_payments_merchant_reference", "merchant_id", "merchant_reference"),
        Index("idx_payments_idempotency", "merchant_id", "idempotency_key"),
    )

    def can_transition_to(self, new_status: PaymentStatus) -> bool:
        current = PaymentStatus(self.status)
        if current == new_status:
            return True
        allowed = VALID_TRANSITIONS.get(current, [])
        return new_status in allowed

    @property
    def amount_display(self) -> str:
        return f"R {self.amount_cents / 100:.2f}"
