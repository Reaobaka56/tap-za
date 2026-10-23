"""Shared FastAPI dependencies: merchant authentication via JWT or API key."""
from datetime import datetime
from typing import Optional
from uuid import UUID
import hmac
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import decode_token, hash_api_key, API_KEY_PREFIX_LEN
from app.models.merchant import Merchant, APIKey


def _unauthorized(detail: str = "Invalid or missing credentials") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


async def _merchant_from_api_key(db: AsyncSession, raw_key: str) -> Optional[Merchant]:
    prefix = raw_key[:API_KEY_PREFIX_LEN]
    result = await db.execute(
        select(APIKey).where(APIKey.key_prefix == prefix, APIKey.revoked_at.is_(None))
    )
    expected = hash_api_key(raw_key)
    for key in result.scalars().all():
        if hmac.compare_digest(key.key_hash, expected):
            key.last_used_at = datetime.utcnow()
            return await db.get(Merchant, key.merchant_id)
    return None


async def get_current_merchant(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Merchant:
    """Authenticate with `Authorization: Bearer <jwt|api key>` or `X-API-Key`."""
    token = x_api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise _unauthorized()

    merchant = None
    if token.startswith("pk_"):
        merchant = await _merchant_from_api_key(db, token)
        if merchant:
            await db.commit()  # persist last_used_at
    else:
        claims = decode_token(token)
        if claims and claims.get("sub"):
            try:
                merchant = await db.get(Merchant, UUID(claims["sub"]))
            except ValueError:
                merchant = None

    if not merchant:
        raise _unauthorized()
    if merchant.status == "suspended":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Merchant account is suspended")
    return merchant


async def get_jwt_merchant(
    authorization: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Merchant:
    """Dashboard-only endpoints (key management) require a login JWT, not an API key."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized()
    claims = decode_token(authorization[7:].strip())
    if not claims or not claims.get("sub"):
        raise _unauthorized()
    try:
        merchant = await db.get(Merchant, UUID(claims["sub"]))
    except ValueError:
        merchant = None
    if not merchant:
        raise _unauthorized()
    return merchant
