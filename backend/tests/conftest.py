import os
import uuid

# Must be set before the app is imported. Tests run on SQLite; production uses PostgreSQL.
_DB_FILE = os.path.join(os.path.dirname(__file__), "test.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
# Set TEST_DATABASE_URL (e.g. postgresql+asyncpg://...) to run the same suite against PostgreSQL.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{_DB_FILE}"
os.environ["ENVIRONMENT"] = "test"
os.environ["MOCK_PROVIDER_SUCCESS_RATE"] = "1.0"
os.environ["ENABLE_WEBHOOK_RETRY_LOOP"] = "false"

import asyncio
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.core.config import get_settings
from app.core.database import init_db
from app.main import app
from app.routers import payments as payments_router


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _database():
    await init_db()
    yield
    if os.path.exists(_DB_FILE):
        os.remove(_DB_FILE)


@pytest.fixture(autouse=True)
def _no_background_delivery(monkeypatch):
    """Never make real outbound HTTP calls from background webhook delivery."""
    async def noop(ids):
        return None
    monkeypatch.setattr(payments_router, "_deliver", noop)


@pytest.fixture
def settings():
    return get_settings()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def make_merchant(client: AsyncClient) -> dict:
    email = f"{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post(
        "/v1/auth/register", json={"email": email, "password": "correct-horse-1", "business_name": "Acme"}
    )
    assert r.status_code == 201, r.text
    jwt = r.json()["access_token"]
    k = await client.post("/v1/auth/api-keys", params={"name": "t"}, headers={"Authorization": f"Bearer {jwt}"})
    assert k.status_code == 201, k.text
    return {
        "email": email,
        "jwt": jwt,
        "key": k.json()["key"],
        "key_id": k.json()["id"],
        "headers": {"Authorization": f"Bearer {k.json()['key']}"},
    }


@pytest_asyncio.fixture
async def merchant(client):
    return await make_merchant(client)


@pytest_asyncio.fixture
async def other_merchant(client):
    return await make_merchant(client)


async def new_payment(client, merchant, amount=19900, **extra):
    r = await client.post(
        "/v1/payments",
        json={"amount": amount, "currency": "ZAR", "methods": ["card"], **extra},
        headers=merchant["headers"],
    )
    return r


async def settled_payment(client, merchant, amount=19900):
    r = await new_payment(client, merchant, amount)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    cb = await client.post(f"/v1/payments/{pid}/callback", params={"success": "true"})
    assert cb.status_code == 200, cb.text
    return cb.json()
