"""Authentication and security utilities."""
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from app.core.config import get_settings

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        return None


def generate_api_key(environment: Optional[str] = None) -> str:
    import secrets
    env = environment or ("live" if settings.ENVIRONMENT == "production" else "test")
    return f"pk_{env}_" + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    """API keys are high-entropy random strings, so a fast SHA-256 is sufficient."""
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()


API_KEY_PREFIX_LEN = 16


def generate_secret_key() -> str:
    import secrets
    return "sk_" + secrets.token_urlsafe(32)
