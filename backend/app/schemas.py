"""Request and response schemas."""
from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from app.models.payment import Payment, PaymentMethod, PaymentStatus


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)  # bcrypt ignores bytes past 72
    business_name: str = Field(min_length=1, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=72)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class APIKeyCreated(BaseModel):
    id: UUID
    name: str
    environment: str
    key: str  # shown once
    key_prefix: str


class APIKeyInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    environment: str
    key_prefix: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class PaymentCreate(BaseModel):
    amount: int = Field(gt=0, le=100_000_000, description="Smallest currency unit (cents)")
    currency: str = "ZAR"
    methods: list[PaymentMethod] = Field(default_factory=lambda: [PaymentMethod.CARD])
    merchant_reference: Optional[str] = Field(default=None, max_length=255)
    customer_email: Optional[EmailStr] = None
    customer_name: Optional[str] = Field(default=None, max_length=255)
    return_url: Optional[str] = Field(default=None, max_length=2000)
    cancel_url: Optional[str] = Field(default=None, max_length=2000)
    description: Optional[str] = Field(default=None, max_length=1000)
    metadata: Optional[dict[str, Any]] = None

    @field_validator("currency")
    @classmethod
    def _zar_only(cls, v: str) -> str:
        if v.upper() != "ZAR":
            raise ValueError("Only ZAR is supported")
        return "ZAR"

    @field_validator("return_url", "cancel_url")
    @classmethod
    def _http_url(cls, v: Optional[str]) -> Optional[str]:
        if v and not v.lower().startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("methods")
    @classmethod
    def _supported_methods(cls, v: list[PaymentMethod]) -> list[PaymentMethod]:
        supported = {PaymentMethod.CARD, PaymentMethod.EFT, PaymentMethod.PAY_BY_BANK}
        if not v or any(m not in supported for m in v):
            raise ValueError("methods must be a non-empty list of: card, eft, pay_by_bank")
        return v


class PaymentOut(BaseModel):
    id: UUID
    status: str
    amount_cents: int
    currency: str
    fee_cents: int
    net_amount_cents: int
    refunded_amount_cents: int
    payment_method: Optional[str] = None
    provider: Optional[str] = None
    provider_reference: Optional[str] = None
    merchant_reference: Optional[str] = None
    customer_email: Optional[str] = None
    description: Optional[str] = None
    checkout_url: Optional[str] = None
    risk_score: Optional[int] = None
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    authorized_at: Optional[datetime] = None
    captured_at: Optional[datetime] = None
    settled_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None

    @classmethod
    def from_payment(cls, p: Payment) -> "PaymentOut":
        data = {name: getattr(p, name) for name in cls.model_fields if name != "metadata"}
        data["metadata"] = p.metadata_ or {}
        return cls(**data)


class PaymentCreateResponse(PaymentOut):
    requires_redirect: bool = False
    error: Optional[str] = None


class WebhookCreate(BaseModel):
    url: str = Field(max_length=2000)
    events: list[str] = Field(default_factory=lambda: ["*"])


class WebhookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    url: str
    events: list[str]
    is_active: bool
    created_at: datetime


class WebhookCreated(WebhookOut):
    secret: str  # signing secret, only returned on creation


class MerchantProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: str
    business_name: str
    status: str
    environment: str
    created_at: datetime


class BalanceOut(BaseModel):
    currency: str = "ZAR"
    balance_cents: int
