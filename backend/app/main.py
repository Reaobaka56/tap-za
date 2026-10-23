"""FastAPI application entrypoint."""
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, init_db
from app.routers import auth, merchants, payments, webhooks
from app.services.webhook import WebhookService

settings = get_settings()
log = logging.getLogger("gateway")

_INSECURE_DEFAULTS = {"dev-secret-key", "whsec_dev_webhook_secret"}


def _check_production_config() -> None:
    if settings.ENVIRONMENT == "production" and (
        settings.SECRET_KEY in _INSECURE_DEFAULTS or settings.WEBHOOK_SECRET in _INSECURE_DEFAULTS
    ):
        raise RuntimeError("Refusing to start in production with default SECRET_KEY / WEBHOOK_SECRET")


async def _webhook_retry_loop() -> None:
    """Retry pending webhook deliveries whose next_retry_at has passed."""
    service = WebhookService()
    while True:
        try:
            async with AsyncSessionLocal() as db:
                ids = await service.due_delivery_ids(db)
            for delivery_id in ids:
                async with AsyncSessionLocal() as db:
                    await service.deliver(db, delivery_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("webhook retry loop error")
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_production_config()
    await init_db()  # create_all for the MVP; switch to Alembic migrations before production
    task = asyncio.create_task(_webhook_retry_loop()) if settings.ENABLE_WEBHOOK_RETRY_LOOP else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title=settings.APP_NAME, version=settings.VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(merchants.router)
app.include_router(payments.router)
app.include_router(webhooks.router)


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "environment": settings.ENVIRONMENT, "version": settings.VERSION}
