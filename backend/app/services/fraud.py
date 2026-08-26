"""Fraud detection and risk scoring."""
from typing import Optional
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from app.models.payment import Payment, PaymentStatus
from app.core.config import get_settings

settings = get_settings()


class FraudEngine:
    """Basic fraud detection with velocity checks and risk scoring."""

    def __init__(self):
        self.rules = [
            self._velocity_check,
            self._amount_check,
            self._duplicate_check,
        ]

    async def evaluate(self, db: AsyncSession, payment: Payment) -> dict:
        """Evaluate payment risk and return score + flags."""
        score = 0
        flags = []

        for rule in self.rules:
            rule_score, rule_flags = await rule(db, payment)
            score += rule_score
            flags.extend(rule_flags)

        # Clamp score 0-100
        score = max(0, min(100, score))

        return {
            "score": score,
            "flags": flags,
            "blocked": score >= 80,
        }

    async def _velocity_check(self, db: AsyncSession, payment: Payment) -> tuple[int, list]:
        """Check payment velocity from same customer/email."""
        flags = []
        score = 0

        # Check last hour
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        result = await db.execute(
            select(func.count(Payment.id)).where(
                and_(
                    Payment.merchant_id == payment.merchant_id,
                    Payment.customer_email == payment.customer_email,
                    Payment.created_at >= one_hour_ago,
                    Payment.status.in_([
                        PaymentStatus.CREATED.value,
                        PaymentStatus.PENDING.value,
                        PaymentStatus.CAPTURED.value
                    ])
                )
            )
        )
        count = result.scalar() or 0

        if count > 10:
            score += 40
            flags.append("velocity:high_hourly")
        elif count > 5:
            score += 20
            flags.append("velocity:elevated")

        # Check last 24 hours
        one_day_ago = datetime.utcnow() - timedelta(days=1)
        result = await db.execute(
            select(func.count(Payment.id)).where(
                and_(
                    Payment.merchant_id == payment.merchant_id,
                    Payment.customer_email == payment.customer_email,
                    Payment.created_at >= one_day_ago,
                )
            )
        )
        daily_count = result.scalar() or 0

        if daily_count > 50:
            score += 30
            flags.append("velocity:high_daily")

        return score, flags

    async def _amount_check(self, db: AsyncSession, payment: Payment) -> tuple[int, list]:
        """Check for suspicious amounts."""
        flags = []
        score = 0

        # Round number amounts are suspicious
        if payment.amount_cents % 10000 == 0:  # Exact R100, R200, etc.
            score += 10
            flags.append("amount:round_number")

        # Very high amounts
        if payment.amount_cents > 500000:  # > R5,000
            score += 15
            flags.append("amount:high_value")

        return score, flags

    async def _duplicate_check(self, db: AsyncSession, payment: Payment) -> tuple[int, list]:
        """Check for duplicate payments."""
        flags = []
        score = 0

        if not payment.merchant_reference:
            return score, flags

        # Check for same reference in last 5 minutes
        five_min_ago = datetime.utcnow() - timedelta(minutes=5)
        result = await db.execute(
            select(func.count(Payment.id)).where(
                and_(
                    Payment.merchant_id == payment.merchant_id,
                    Payment.merchant_reference == payment.merchant_reference,
                    Payment.created_at >= five_min_ago,
                    Payment.id != payment.id
                )
            )
        )
        count = result.scalar() or 0

        if count > 0:
            score += 25
            flags.append("duplicate:recent_reference")

        return score, flags
