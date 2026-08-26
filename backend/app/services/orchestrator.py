"""Payment orchestrator - routes transactions to underlying providers."""
import random
import uuid
from typing import Optional
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.payment import Payment, PaymentStatus, PaymentMethod
from app.services.payment_engine import PaymentEngine
from app.services.ledger import LedgerService
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
        """Create payment and route to best available provider."""

        # Select primary method (first available)
        primary_method = methods[0] if methods else PaymentMethod.CARD

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

        # Route to provider
        provider = self.PROVIDERS.get(primary_method, MockCardProvider())
        payment.provider = provider.name

        result = await provider.process(payment)

        if result.success:
            await self.engine.transition_status(
                db, payment, PaymentStatus.PENDING, result.provider_reference
            )

            # Build checkout URL
            checkout_url = f"http://localhost:8000/v1/payments/{payment.id}/checkout"
            payment.checkout_url = checkout_url

            await db.flush()

            return {
                "payment": payment,
                "checkout_url": checkout_url,
                "provider": provider.name,
                "requires_redirect": result.requires_redirect,
            }
        else:
            await self.engine.transition_status(db, payment, PaymentStatus.FAILED)
            await db.flush()

            return {
                "payment": payment,
                "error": result.error_message,
            }

    async def process_callback(
        self,
        db: AsyncSession,
        payment: Payment,
        success: bool,
        provider_reference: Optional[str] = None
    ) -> Payment:
        """Process provider callback and update payment state."""

        if payment.status in [PaymentStatus.CAPTURED.value, PaymentStatus.SETTLED.value]:
            return payment  # Already processed

        if success:
            # Transition to captured
            payment, _ = await self.engine.transition_status(
                db, payment, PaymentStatus.CAPTURED, provider_reference
            )

            # Record in ledger
            await self.ledger.record_payment_captured(db, payment, payment.fee_cents)

            # Auto-settle for MVP
            payment, _ = await self.engine.transition_status(
                db, payment, PaymentStatus.SETTLED
            )
        else:
            payment, _ = await self.engine.transition_status(
                db, payment, PaymentStatus.FAILED
            )

        await db.flush()
        return payment

    async def process_refund(
        self,
        db: AsyncSession,
        payment: Payment,
        refund_amount_cents: Optional[int] = None
    ) -> Payment:
        """Process refund for a settled payment."""

        if payment.status != PaymentStatus.SETTLED.value:
            raise ValueError("Can only refund settled payments")

        refund_amount = refund_amount_cents or payment.amount_cents

        if refund_amount > (payment.amount_cents - payment.refunded_amount_cents):
            raise ValueError("Refund amount exceeds available balance")

        # Update payment
        payment.refunded_amount_cents += refund_amount

        if payment.refunded_amount_cents >= payment.amount_cents:
            new_status = PaymentStatus.REFUNDED
        else:
            new_status = PaymentStatus.PARTIALLY_REFUNDED

        payment, _ = await self.engine.transition_status(db, payment, new_status)

        # Record in ledger
        await self.ledger.record_payment_refunded(db, payment, refund_amount)

        await db.flush()
        return payment
