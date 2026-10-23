"""Merchant profile and balance."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.deps import get_current_merchant
from app.models.merchant import Merchant
from app.schemas import BalanceOut, MerchantProfile
from app.services.ledger import LedgerService

router = APIRouter(prefix="/v1/merchant", tags=["merchant"])


@router.get("/profile", response_model=MerchantProfile)
async def profile(merchant: Merchant = Depends(get_current_merchant)):
    return merchant


@router.get("/balance", response_model=BalanceOut)
async def balance(merchant: Merchant = Depends(get_current_merchant), db: AsyncSession = Depends(get_db)):
    cents = await LedgerService().get_merchant_balance(db, merchant.id)
    await db.commit()  # the balance account may have just been created
    return BalanceOut(balance_cents=cents)
