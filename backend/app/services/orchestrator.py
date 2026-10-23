"""Payment orchestrator - routes transactions to underlying providers."""
import random
import uuid
from typing import Optional
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.payment import Payment, PaymentStatus, PaymentMethod
from app.services.payment_engine import PaymentEngine
from app.services.ledger import LedgerService
from app.services.fraud import FraudEngine
from app.core.config import get_settings

settings = get_settings()


class ProviderResult:
    def __init__(
        self,
        success: bool,
        provider_reference: Optional[str] = None,
        redirect_url: Optional[str] = None,
        error_message: Optional[str] = None,
        requires_redirect: bool = False
    ):
        self.success = success
        self.provider_reference = provider_reference
        self.redirect_url = redirect_url
        self.error_message = error_message
        self.requires_redirect = requires_redirect


class MockCardProvider:
    """Mock card acquirer for testing."""
    name = "mock_card"

    async def process(self, payment: Payment) -> ProviderResult:
        success = random.random() < settings.MOCK_PROVIDER_SUCCESS_RATE
        ref = f"MOCK_CARD_{uuid.uuid4().hex[:12]}"

        if success:
            return ProviderResult(
                success=True,
                provider_reference=ref,
                redirect_url=f"/checkout/card/{payment.id}",
                requires_redirect=True
            )
        return ProviderResult(
            success=False,
            provider_reference=ref,
            error_message="Card declined by issuer"
        )

    async def capture(self, payment: Payment) -> ProviderResult:
        return ProviderResult(success=True, provider_reference=payment.provider_reference)


class MockEFTProvider:
    """Mock EFT provider (like Ozow)."""
    name = "mock_eft"

    async def process(self, payment: Payment) -> ProviderResult:
        ref = f"MOCK_EFT_{uuid.uuid4().hex[:12]}"
        return ProviderResult(
            success=True,
            provider_reference=ref,
            redirect_url=f"/checkout/eft/{payment.id}",
            requires_redirect=True
        )

    async def capture(self, payment: Payment) -> ProviderResult:
        success = random.random() < settings.MOCK_PROVIDER_SUCCESS_RATE
        if success:
            return ProviderResult(success=True, provider_reference=payment.provider_reference)
        return ProviderResult(success=False, error_message="EFT transfer failed")


class MockPayByBankProvider:
    """Mock pay-by-bank provider."""
    name = "mock_pay_by_bank"

    async def process(self, payment: Payment) -> ProviderResult:
        ref = f"MOCK_PBB_{uuid.uuid4().hex[:12]}"
        return ProviderResult(
            success=True,
            provider_reference=ref,
            redirect_url=f"/checkout/bank/{payment.id}",
            requires_redirect=True
        )

    async def capture(self, payment: Payment) -> ProviderResult:
        success = random.random() < settings.MOCK_PROVIDER_SUCCESS_RATE
        if success:
            return ProviderResult(success=True, provider_reference=payment.provider_reference)
        return ProviderResult(success=False, error_message="Bank transfer declined")


class IdempotencyConflict(ValueError):
    """Same Idempotency-Key reused with different request parameters."""


class PaymentOrchestrator:
    """Routes payments to appropriate providers."""

    PROVIDERS = {
        PaymentMethod.CARD: MockCardProvider(),
        PaymentMethod.EFT: MockEFTProvider(),
        PaymentMethod.PAY_BY_BANK: MockPayByBankProvider(),
    }

    def __init__(self):
        self.engine = PaymentEngine()
        self.ledger = LedgerService()
        self.fraud = FraudEngine()

    async def create_and_route_payment(
        self,
        db: AsyncSession,
        merchant_id: uuid.UUID,
        amount_cents: int,
        currency: str,
        methods: list[PaymentMethod],
        merchant_reference: Optional[str],
        customer_email: Optional[str],
        customer_name: Optional[str],
        return_url: Optional[str],
        cancel_url: Optional[str],
        description: Optional[str],
        idempotency_key: Optional[str],
        metadata: Optional[dict],
    ) -> dict:
        """Create payment and route to best available provider.

        The result dict always has "payment" and "created" (False when an
        idempotent replay returned an existing payment). On failure it also
        has "error".
        """

        # Select primary method (first available)
        primary_method = methods[0] if methods else PaymentMethod.CARD

        # Idempotent replay: return the original payment without re-routing
        if idempotency_key:
            existing = await self.engine.find_by_idempotency_key(db, merchant_id, idempotency_key)
            if existing:
                if existing.amount_cents != amount_cents or existing.currency != currency:
                    raise IdempotencyConflict(
                        "Idempotency-Key was already used with different request parameters"
                    )
                return self._result_for_existing(existing)

        # Create payment
        payment = await self.engine.create_payment(
            db=db,
            merchant_id=merchant_id,
            amount_cents=amount_cents,
            currency=currency,
            payment_method=primary_method,
            merchant_reference=merchant_reference,
            customer_email=customer_email,
            customer_name=customer_name,
            return_url=return_url,
            cancel_url=cancel_url,
            description=description,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

        # Risk evaluation before any money-moving step
        risk = await self.fraud.evaluate(db, payment)
        payment.risk_score = risk["score"]
        payment.risk_flags = risk["flags"]
        if risk["blocked"]:
            await self.engine.transition_status(db, payment, PaymentStatus.FAILED)
            await db.flush()
            return {"payment": payment, "created": True, "error": "Payment blocked by risk engine"}

        # Route to provider
        provider = self.PROVIDERS.get(primary_method, MockCardProvider())
        payment.provider = provider.name

        result = await provider.process(payment)

        if result.success:
            await self.engine.transition_status(
                db, payment, PaymentStatus.PENDING, result.provider_reference
            )
            payment.checkout_url = f"{settings.PUBLIC_BASE_URL}/v1/payments/{payment.id}/checkout"
            await db.flush()

            return {
                "payment": payment,
                "created": True,
                "checkout_url": payment.checkout_url,
                "provider": provider.name,
                "requires_redirect": result.requires_redirect,
            }

        await self.engine.transition_status(db, payment, PaymentStatus.FAILED, result.provider_reference)
        await db.flush()
        return {"payment": payment, "created": True, "error": result.error_message}

    @staticmethod
    def _result_for_existing(payment: Payment) -> dict:
        out = {"payment": payment, "created": False, "provider": payment.provider}
        if payment.checkout_url:
            out["checkout_url"] = payment.checkout_url
            out["requires_redirect"] = payment.status == PaymentStatus.PENDING.value
        if payment.status == PaymentStatus.FAILED.value:
            out["error"] = "Payment failed"
        return out

    async def process_callback(
        self,
        db: AsyncSession,
        payment: Payment,
        success: bool,
        provider_reference: Optional[str] = None
    ) -> Payment:
        """Process provider callback and update payment state.

        Replays of a callback that matches the payment's current outcome are
        no-ops. A callback that contradicts a final state raises ValueError.
        """
        done = (PaymentStatus.CAPTURED.value, PaymentStatus.SETTLED.value,
                PaymentStatus.REFUNDED.value, PaymentStatus.PARTIALLY_REFUNDED.value,
                PaymentStatus.REFUND_PENDING.value)
        if payment.status in done:
            if success:
                return payment  # already processed
            raise ValueError(f"Payment is already {payment.status}")
        if payment.status == PaymentStatus.FAILED.value and not success:
            return payment

        if success:
            if payment.status != PaymentStatus.PENDING.value:
                raise ValueError(f"Cannot capture a payment in state {payment.status}")
            # PENDING -> AUTHORIZED -> CAPTURED -> SETTLED (auto-settle for MVP)
            await self.engine.transition_status(db, payment, PaymentStatus.AUTHORIZED, provider_reference)
            await self.engine.transition_status(db, payment, PaymentStatus.CAPTURED)
            await self.ledger.record_payment_captured(db, payment, payment.fee_cents)
            await self.engine.transition_status(db, payment, PaymentStatus.SETTLED)
        else:
            await self.engine.transition_status(db, payment, PaymentStatus.FAILED)

        await db.flush()
        return payment

    async def cancel_payment(self, db: AsyncSession, payment: Payment) -> Payment:
        """Customer cancelled at checkout."""
        if payment.status == PaymentStatus.CANCELLED.value:
            return payment
        await self.engine.transition_status(db, payment, PaymentStatus.CANCELLED)
        await db.flush()
        return payment

    async def process_refund(
        self,
        db: AsyncSession,
        payment: Payment,
        refund_amount_cents: Optional[int] = None
    ) -> Payment:
        """Process a full or partial refund for a settled payment."""

        if payment.status not in (PaymentStatus.SETTLED.value, PaymentStatus.PARTIALLY_REFUNDED.value):
            raise ValueError("Can only refund settled payments")

        remaining = payment.amount_cents - payment.refunded_amount_cents
        refund_amount = remaining if refund_amount_cents is None else refund_amount_cents

        if refund_amount <= 0:
            raise ValueError("Refund amount must be positive")
        if refund_amount > remaining:
            raise ValueError("Refund amount exceeds available balance")

        merchant_balance = await self.ledger.get_merchant_balance(db, payment.merchant_id)
        if refund_amount > merchant_balance:
            raise ValueError("Insufficient merchant balance to fund this refund")

        payment.refunded_amount_cents += refund_amount
        new_status = (
            PaymentStatus.REFUNDED
            if payment.refunded_amount_cents >= payment.amount_cents
            else PaymentStatus.PARTIALLY_REFUNDED
        )
        await self.engine.transition_status(db, payment, new_status)
        await self.ledger.record_payment_refunded(db, payment, refund_amount)

        await db.flush()
        return payment
