"""Application configuration and settings."""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    APP_NAME: str = "African Payment Gateway"
    VERSION: str = "1.0.0"
    DATABASE_URL: str = "postgresql+asyncpg://gateway:gateway_secret@localhost:5432/payment_gateway"
    REDIS_URL: str = "redis://localhost:6379/0"
    SECRET_KEY: str = "dev-secret-key"
    ENVIRONMENT: str = "development"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 1 day
    WEBHOOK_SECRET: str = "whsec_dev_webhook_secret"
    GATEWAY_FEE_PERCENT: float = 2.5
    GATEWAY_FEE_FIXED_CENTS: int = 150  # R1.50

    # Mock provider settings
    MOCK_PROVIDER_SUCCESS_RATE: float = 0.85

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
