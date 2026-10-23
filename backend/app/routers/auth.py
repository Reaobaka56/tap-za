"""Merchant registration, login and API-key management."""
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from app.core.database import get_db
from app.core.deps import get_jwt_merchant
from app.core.security import (
    API_KEY_PREFIX_LEN, create_access_token, generate_api_key, hash_api_key,
    hash_password, verify_password,
)
from app.models.merchant import APIKey, Merchant
from app.schemas import (
    APIKeyCreated, APIKeyInfo, LoginRequest, MerchantProfile, RegisterRequest, TokenResponse,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Verified against when the email is unknown, so login timing doesn't reveal which emails exist.
_DUMMY_HASH = hash_password("not-a-real-password")


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    email = body.email.lower()
    existing = await db.execute(select(Merchant).where(Merchant.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    merchant = Merchant(
        email=email,
        password_hash=hash_password(body.password),
        business_name=body.business_name,
    )
    db.add(merchant)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    return TokenResponse(access_token=create_access_token({"sub": str(merchant.id)}))


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Merchant).where(Merchant.email == body.email.lower()))
    merchant = result.scalar_one_or_none()
    ok = verify_password(body.password, merchant.password_hash if merchant else _DUMMY_HASH)
    if not merchant or not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    return TokenResponse(access_token=create_access_token({"sub": str(merchant.id)}))


@router.get("/me", response_model=MerchantProfile)
async def me(merchant: Merchant = Depends(get_jwt_merchant)):
    return merchant


@router.post("/api-keys", response_model=APIKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    name: str,
    environment: Literal["test", "live"] = "test",
    merchant: Merchant = Depends(get_jwt_merchant),
    db: AsyncSession = Depends(get_db),
):
    """Create an API key. The full key is returned once and cannot be retrieved again."""
    raw = generate_api_key(environment)
    key = APIKey(
        merchant_id=merchant.id,
        key_prefix=raw[:API_KEY_PREFIX_LEN],
        key_hash=hash_api_key(raw),
        name=name[:100],
        environment=environment,
    )
    db.add(key)
    await db.commit()
    return APIKeyCreated(
        id=key.id, name=key.name, environment=key.environment, key=raw, key_prefix=key.key_prefix
    )


@router.get("/api-keys", response_model=list[APIKeyInfo])
async def list_api_keys(merchant: Merchant = Depends(get_jwt_merchant), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(APIKey).where(APIKey.merchant_id == merchant.id).order_by(APIKey.created_at.desc())
    )
    return result.scalars().all()


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: UUID, merchant: Merchant = Depends(get_jwt_merchant), db: AsyncSession = Depends(get_db)
):
    key = await db.get(APIKey, key_id)
    if not key or key.merchant_id != merchant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    if not key.revoked_at:
        key.revoked_at = datetime.utcnow()
        await db.commit()
