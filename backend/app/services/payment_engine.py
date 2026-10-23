"""Payment engine - state machine, idempotency, fee calculation."""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from app.models.payment import Payment, PaymentStatus, PaymentMethod
from app.models.merchant import Merchant
from app.core.config import get_settings

settings = get_settings()


class PaymentEngine:
    """Core payment processing engine."""

    @staticmethod
    def calculate_fees(amount_cents: int) -> tuple[int, int]:
        """Calculate gateway fees. Returns (fee_cents, net_cents)."""
        percent_fee = (Decimal(amount_cents) * Decimal(str(settings.GATEWAY_FEE_PERCENT)) / 100)
        fee = int(percent_fee.quantize(Decimal(1), rounding=ROUND_HALF_UP)) + settings.GATEWAY_FEE_FIXED_CENTS
        net = amount_cents - fee
        return fee, net

    async def find_by_idempotency_key(
        self, db: AsyncSession, merchant_id: UUID, idempotency_key: str
    ) -> Optional[Payment]:
        result = await db.execute(
            select(Payment).where(
                and_(Payment.merchant_id == merchant_id, Payment.idempotency_key == idempotency_key)
            )
        )
        return result.scalar_one_or_none()

    async def create_payment(
        self,
        db: AsyncSession,
        merchant_id: UUID,
        amount_cents: int,
        currency: str,
        payment_method: Optional[PaymentMethod],
        merchant_reference: Optional[str],
        customer_email: Optional[str],
        customer_name: Optional[str],
        return_url: Optional[str],
        cancel_url: Optional[str],
        description: Optional[str],
        idempotency_key: Optional[str],
        metadata: Optional[dict],
    ) -> Payment:
        """Create a new payment with idempotency check."""

        # Check idempotency
        if idempotency_key:
            existing = await db.execute(
                select(Payment).where(
                    and_(
                        Payment.merchant_id == merchant_id,
                        Payment.idempotency_key == idempotency_key
                    )
                )
            )
            existing_payment = existing.scalar_one_or_none()
            if existing_payment:
                return existing_payment

        fee_cents, net_cents = self.calculate_fees(amount_cents)

        payment = Payment(
            merchant_id=merchant_id,
            amount_cents=amount_cents,
            currency=currency,
            fee_cents=fee_cents,
            net_amount_cents=net_cents,
            payment_method=payment_method.value if payment_method else None,
            merchant_reference=merchant_reference,
            customer_email=customer_email,
            customer_name=customer_name,
            return_url=return_url,
            cancel_url=cancel_url,
            description=description,
            idempotency_key=idempotency_key,
            metadata_=metadata or {},
            status=PaymentStatus.CREATED.value,
        )

        db.add(payment)
        await db.flush()
        return payment

    async def transition_status(
        self,
        db: AsyncSession,
        payment: Payment,
        new_status: PaymentStatus,
        provider_reference: Optional[str] = None,
    ) -> tuple[Payment, str]:
        """Transition payment to new state with validation. Returns (payment, old_status)."""

        if not payment.can_transition_to(new_status):
            raise ValueError(
                f"Invalid state transition from {payment.status} to {new_status.value}"
            )

        old_status = payment.status
        payment.status = new_status.value

        if provider_reference:
            payment.provider_reference = provider_reference

        # Set timestamps
        now = datetime.utcnow()
        if new_status == PaymentStatus.AUTHORIZED:
            payment.authorized_at = now
        elif new_status == PaymentStatus.CAPTURED:
            payment.captured_at = now
        elif new_status == PaymentStatus.SETTLED:
            payment.settled_at = now
        elif new_status == PaymentStatus.FAILED:
            payment.failed_at = now

        await db.flush()
        return payment, old_status

    async def get_payment(
        self,
        db: AsyncSession,
        payment_id: UUID,
        merchant_id: Optional[UUID] = None,
        for_update: bool = False,
    ) -> Optional[Payment]:
        """Get payment by ID, optionally scoped to merchant. for_update row-locks it."""
        query = select(Payment).where(Payment.id == payment_id)
        if merchant_id:
            query = query.where(Payment.merchant_id == merchant_id)
        if for_update:
            query = query.with_for_update()

        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def list_payments(
        self,
        db: AsyncSession,
        merchant_id: UUID,
        status: Optional[PaymentStatus] = None,
        limit: int = 50,
        offset: int = 0
    ) -> list[Payment]:
        """List payments for a merchant."""
        query = select(Payment).where(Payment.merchant_id == merchant_id)

        if status:
            query = query.where(Payment.status == status.value)

        query = query.order_by(Payment.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(query)
        return result.scalars().all()
