"""Double-entry ledger system - the financial source of truth."""
import uuid
from datetime import datetime
from enum import Enum as PyEnum
from sqlalchemy import Column, String, BigInteger, DateTime, Text, ForeignKey, Index, Enum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, validates
from app.core.database import Base


class AccountType(str, PyEnum):
    ASSET = "asset"           # Money we hold (clearing, reserves)
    LIABILITY = "liability"   # Money we owe (merchant balances, customer deposits)
    REVENUE = "revenue"       # Fees earned
    EXPENSE = "expense"       # Costs incurred
    EQUITY = "equity"         # Owner equity


class LedgerEntryType(str, PyEnum):
    PAYMENT_CREATED = "payment_created"
    PAYMENT_AUTHORIZED = "payment_authorized"
    PAYMENT_CAPTURED = "payment_captured"
    PAYMENT_SETTLED = "payment_settled"
    PAYMENT_FAILED = "payment_failed"
    PAYMENT_REFUNDED = "payment_refunded"
    FEE_CHARGED = "fee_charged"
    PAYOUT = "payout"
    ADJUSTMENT = "adjustment"


class Account(Base):
    __tablename__ = "accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id = Column(UUID(as_uuid=True), ForeignKey("merchants.id"), nullable=True)  # NULL for system accounts
    name = Column(String(100), nullable=False)
    account_type = Column(Enum(AccountType), nullable=False)
    currency = Column(String(3), default="ZAR")
    balance_cents = Column(BigInteger, default=0)  # Running balance

    # System accounts have no merchant
    is_system = Column(String(1), default="f")

    created_at = Column(DateTime, default=datetime.utcnow)

    merchant = relationship("Merchant", back_populates="accounts")
    debits = relationship("LedgerEntry", foreign_keys="LedgerEntry.debit_account_id", back_populates="debit_account")
    credits = relationship("LedgerEntry", foreign_keys="LedgerEntry.credit_account_id", back_populates="credit_account")

    __table_args__ = (
        Index("idx_accounts_merchant", "merchant_id", "account_type"),
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_id = Column(UUID(as_uuid=True), ForeignKey("payments.id"), nullable=True)

    # Double-entry: every entry has debit and credit
    debit_account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    credit_account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)

    amount_cents = Column(BigInteger, nullable=False)
    currency = Column(String(3), default="ZAR")
    entry_type = Column(Enum(LedgerEntryType), nullable=False)
    description = Column(Text, nullable=True)

    # Running balances after this entry
    debit_balance_after = Column(BigInteger, nullable=True)
    credit_balance_after = Column(BigInteger, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    payment = relationship("Payment", back_populates="ledger_entries")
    debit_account = relationship("Account", foreign_keys=[debit_account_id], back_populates="debits")
    credit_account = relationship("Account", foreign_keys=[credit_account_id], back_populates="credits")

    __table_args__ = (
        Index("idx_ledger_payment", "payment_id"),
        Index("idx_ledger_debit_account", "debit_account_id", "created_at"),
        Index("idx_ledger_credit_account", "credit_account_id", "created_at"),
    )

    @validates("amount_cents")
    def validate_amount(self, key, amount):
        if amount <= 0:
            raise ValueError("Ledger entry amount must be positive")
        return amount
