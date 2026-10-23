"""Ledger service - the financial source of truth.

Every movement of money creates corresponding debit and credit entries.
Example: R500 customer payment
  Customer (Asset): -R500  (credit)
  Gateway Clearing (Asset): +R500  (debit)
  Merchant (Liability): +R485  (credit)
  Gateway Fee (Revenue): +R15  (credit)
"""
from typing import Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.models.ledger import Account, AccountType, LedgerEntry, LedgerEntryType
from app.models.payment import Payment
from app.core.config import get_settings

settings = get_settings()


class LedgerService:
    """Manages double-entry bookkeeping."""

    # System account names
    CLEARING_ACCOUNT = "Gateway Clearing"
    FEE_REVENUE_ACCOUNT = "Gateway Fee Revenue"
    RESERVE_ACCOUNT = "Reserve Account"

    async def get_or_create_system_accounts(self, db: AsyncSession) -> dict:
        """Get or create the core system accounts."""
        accounts = {}

        account_configs = [
            (self.CLEARING_ACCOUNT, AccountType.ASSET),
            (self.FEE_REVENUE_ACCOUNT, AccountType.REVENUE),
            (self.RESERVE_ACCOUNT, AccountType.ASSET),
        ]

        for name, acc_type in account_configs:
            result = await db.execute(select(Account).where(Account.name == name, Account.is_system == "t").with_for_update())
            account = result.scalar_one_or_none()

            if not account:
                account = Account(
                    name=name,
                    account_type=acc_type,
                    is_system="t",
                    currency="ZAR"
                )
                db.add(account)
                await db.flush()

            accounts[name] = account

        return accounts

    async def get_or_create_merchant_account(self, db: AsyncSession, merchant_id: UUID) -> Account:
        """Get or create the merchant's liability account."""
        result = await db.execute(
            select(Account).where(
                Account.merchant_id == merchant_id,
                Account.account_type == AccountType.LIABILITY
            ).with_for_update()
        )
        account = result.scalar_one_or_none()

        if not account:
            account = Account(
                merchant_id=merchant_id,
                name="Merchant Balance",
                account_type=AccountType.LIABILITY,
                currency="ZAR"
            )
            db.add(account)
            await db.flush()

        return account

    async def record_payment_captured(
        self,
        db: AsyncSession,
        payment: Payment,
        fee_cents: int
    ) -> list[LedgerEntry]:
        """Record the ledger entries when a payment is captured.

        Debits:
          - Gateway Clearing: +amount (we now hold the money)
        Credits:
          - Merchant Liability: +(amount - fee) (we owe merchant)
          - Gateway Fee Revenue: +fee (our revenue)
        """
        system_accounts = await self.get_or_create_system_accounts(db)
        merchant_account = await self.get_or_create_merchant_account(db, payment.merchant_id)

        net_amount = payment.amount_cents - fee_cents
        entries = []

        # 1. Debit clearing account (we received money)
        entry1 = await self._create_entry(
            db=db,
            payment_id=payment.id,
            debit_account=system_accounts[self.CLEARING_ACCOUNT],
            credit_account=merchant_account,
            amount_cents=net_amount,
            entry_type=LedgerEntryType.PAYMENT_CAPTURED,
            description=f"Payment {payment.id} captured - net amount"
        )
        entries.append(entry1)

        # 2. Fee entry
        if fee_cents > 0:
            entry2 = await self._create_entry(
                db=db,
                payment_id=payment.id,
                debit_account=system_accounts[self.CLEARING_ACCOUNT],
                credit_account=system_accounts[self.FEE_REVENUE_ACCOUNT],
                amount_cents=fee_cents,
                entry_type=LedgerEntryType.FEE_CHARGED,
                description=f"Payment {payment.id} gateway fee"
            )
            entries.append(entry2)

        await db.flush()
        return entries

    async def record_payment_refunded(
        self,
        db: AsyncSession,
        payment: Payment,
        refund_amount_cents: int
    ) -> list[LedgerEntry]:
        """Record refund ledger entries.

        Reverse the original capture partially or fully.
        """
        system_accounts = await self.get_or_create_system_accounts(db)
        merchant_account = await self.get_or_create_merchant_account(db, payment.merchant_id)

        entries = []

        # Debit merchant account (reduce what we owe them)
        entry = await self._create_entry(
            db=db,
            payment_id=payment.id,
            debit_account=merchant_account,
            credit_account=system_accounts[self.CLEARING_ACCOUNT],
            amount_cents=refund_amount_cents,
            entry_type=LedgerEntryType.PAYMENT_REFUNDED,
            description=f"Payment {payment.id} refund"
        )
        entries.append(entry)

        await db.flush()
        return entries

    async def _create_entry(
        self,
        db: AsyncSession,
        debit_account: Account,
        credit_account: Account,
        amount_cents: int,
        entry_type: LedgerEntryType,
        description: str,
        payment_id: Optional[UUID] = None
    ) -> LedgerEntry:
        """Create a double-entry ledger record and update running balances."""

        # Update running balances
        # Asset/Expense: debit increases, credit decreases
        # Liability/Revenue/Equity: debit decreases, credit increases

        def balance_change(account_type: AccountType, is_debit: bool) -> int:
            if account_type in (AccountType.ASSET, AccountType.EXPENSE):
                return amount_cents if is_debit else -amount_cents
            else:
                return -amount_cents if is_debit else amount_cents

        debit_change = balance_change(debit_account.account_type, True)
        credit_change = balance_change(credit_account.account_type, False)

        debit_account.balance_cents += debit_change
        credit_account.balance_cents += credit_change

        entry = LedgerEntry(
            payment_id=payment_id,
            debit_account_id=debit_account.id,
            credit_account_id=credit_account.id,
            amount_cents=amount_cents,
            entry_type=entry_type,
            description=description,
            debit_balance_after=debit_account.balance_cents,
            credit_balance_after=credit_account.balance_cents,
        )

        db.add(entry)
        return entry

    async def get_merchant_balance(self, db: AsyncSession, merchant_id: UUID) -> int:
        """Get current merchant balance in cents."""
        account = await self.get_or_create_merchant_account(db, merchant_id)
        return account.balance_cents

    async def get_ledger_for_payment(self, db: AsyncSession, payment_id: UUID) -> list[LedgerEntry]:
        """Get all ledger entries for a payment."""
        result = await db.execute(
            select(LedgerEntry)
            .where(LedgerEntry.payment_id == payment_id)
            .order_by(LedgerEntry.created_at)
        )
        return result.scalars().all()
