"""End-to-end API tests: auth, payment lifecycle, ledger invariants, refunds, tenancy, webhooks."""
import json
import uuid
import pytest
from sqlalchemy import select
from app.core.database import AsyncSessionLocal
from app.models.ledger import Account, AccountType
from app.models.webhook import WebhookDelivery
from app.services.webhook import WebhookService
from tests.conftest import new_payment, settled_payment


# ---------- auth ----------

async def test_register_login_and_duplicate(client):
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    body = {"email": email, "password": "correct-horse-1", "business_name": "Acme"}
    assert (await client.post("/v1/auth/register", json=body)).status_code == 201
    assert (await client.post("/v1/auth/register", json=body)).status_code == 409
    assert (await client.post("/v1/auth/login", json={"email": email, "password": "correct-horse-1"})).status_code == 200
    assert (await client.post("/v1/auth/login", json={"email": email, "password": "wrong-password"})).status_code == 401
    assert (await client.post("/v1/auth/login", json={"email": "nobody@example.com", "password": "x"})).status_code == 401


async def test_short_password_rejected(client):
    r = await client.post("/v1/auth/register", json={"email": "a@b.co", "password": "short", "business_name": "x"})
    assert r.status_code == 422


async def test_requires_auth(client):
    assert (await client.get("/v1/payments")).status_code == 401
    assert (await client.get("/v1/payments", headers={"Authorization": "Bearer pk_test_nope"})).status_code == 401
    assert (await client.get("/v1/merchant/balance", headers={"Authorization": "Bearer garbage"})).status_code == 401


async def test_api_key_lifecycle(client, merchant):
    r = await client.get("/v1/merchant/profile", headers=merchant["headers"])
    assert r.status_code == 200
    assert (await client.get("/v1/merchant/profile", headers={"X-API-Key": merchant["key"]})).status_code == 200
    # API keys cannot mint more keys; only a login JWT can
    r = await client.post("/v1/auth/api-keys", params={"name": "x"}, headers=merchant["headers"])
    assert r.status_code == 401
    jwt = {"Authorization": f"Bearer {merchant['jwt']}"}
    keys = (await client.get("/v1/auth/api-keys", headers=jwt)).json()
    assert keys and all("key" not in k and "key_hash" not in k for k in keys)
    assert (await client.delete(f"/v1/auth/api-keys/{merchant['key_id']}", headers=jwt)).status_code == 204
    assert (await client.get("/v1/merchant/profile", headers=merchant["headers"])).status_code == 401


# ---------- payments ----------

async def test_create_payment_pending(client, merchant):
    r = await new_payment(client, merchant, 19900, merchant_reference="ORDER-1", metadata={"k": "v"})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["status"] == "pending"
    assert d["fee_cents"] == 648 and d["net_amount_cents"] == 19252
    assert d["requires_redirect"] is True and d["checkout_url"].endswith("/checkout")
    assert d["metadata"] == {"k": "v"}


@pytest.mark.parametrize("payload", [
    {"amount": 0, "currency": "ZAR"},
    {"amount": -5, "currency": "ZAR"},
    {"amount": 100, "currency": "ZAR"},          # below gateway fee
    {"amount": 5000, "currency": "USD"},
    {"amount": 5000, "currency": "ZAR", "methods": ["wallet"]},
    {"amount": 5000, "currency": "ZAR", "return_url": "javascript:alert(1)"},
])
async def test_invalid_payment_rejected(client, merchant, payload):
    r = await client.post("/v1/payments", json=payload, headers=merchant["headers"])
    assert r.status_code == 422


async def test_idempotency(client, merchant):
    h = {**merchant["headers"], "Idempotency-Key": "order-123"}
    body = {"amount": 12345, "currency": "ZAR"}
    a = await client.post("/v1/payments", json=body, headers=h)
    b = await client.post("/v1/payments", json=body, headers=h)
    assert a.status_code == 201 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"]
    c = await client.post("/v1/payments", json={"amount": 99999, "currency": "ZAR"}, headers=h)
    assert c.status_code == 409
    lst = (await client.get("/v1/payments", headers=merchant["headers"])).json()
    assert len([p for p in lst if p["id"] == a.json()["id"]]) == 1


async def test_idempotency_keys_are_per_merchant(client, merchant, other_merchant):
    body = {"amount": 12345, "currency": "ZAR"}
    a = await client.post("/v1/payments", json=body, headers={**merchant["headers"], "Idempotency-Key": "k1"})
    b = await client.post("/v1/payments", json=body, headers={**other_merchant["headers"], "Idempotency-Key": "k1"})
    assert a.status_code == 201 and b.status_code == 201 and a.json()["id"] != b.json()["id"]


async def test_tenant_isolation(client, merchant, other_merchant):
    pid = (await new_payment(client, merchant)).json()["id"]
    assert (await client.get(f"/v1/payments/{pid}", headers=other_merchant["headers"])).status_code == 404
    assert (await client.post(f"/v1/payments/{pid}/refund", headers=other_merchant["headers"])).status_code == 404
    assert all(p["id"] != pid for p in (await client.get("/v1/payments", headers=other_merchant["headers"])).json())


async def test_declined_payment(client, merchant, settings, monkeypatch):
    monkeypatch.setattr(settings, "MOCK_PROVIDER_SUCCESS_RATE", 0.0)
    r = await new_payment(client, merchant, 7777)
    assert r.status_code == 402
    assert r.json()["status"] == "failed" and "declined" in r.json()["error"].lower()


async def test_list_filter_by_status(client, merchant):
    await settled_payment(client, merchant, 11111)
    await new_payment(client, merchant, 22222)
    settled = (await client.get("/v1/payments", params={"status": "settled"}, headers=merchant["headers"])).json()
    assert settled and all(p["status"] == "settled" for p in settled)
    assert (await client.get("/v1/payments", params={"status": "bogus"}, headers=merchant["headers"])).status_code == 422


# ---------- ledger ----------

async def _balances():
    async with AsyncSessionLocal() as db:
        accounts = (await db.execute(select(Account))).scalars().all()
    by = lambda *t: sum(a.balance_cents for a in accounts if a.account_type in t)
    return by(AccountType.ASSET, AccountType.EXPENSE), by(AccountType.LIABILITY, AccountType.REVENUE, AccountType.EQUITY)


async def test_capture_updates_balance_and_ledger(client, merchant):
    p = await settled_payment(client, merchant, 19900)
    assert p["status"] == "settled"
    bal = (await client.get("/v1/merchant/balance", headers=merchant["headers"])).json()
    assert bal["balance_cents"] == 19252
    entries = (await client.get(f"/v1/payments/{p['id']}/ledger", headers=merchant["headers"])).json()
    assert sorted(e["entry_type"] for e in entries) == ["fee_charged", "payment_captured"]
    assert sum(e["amount_cents"] for e in entries) == 19900
    assets, claims = await _balances()
    assert assets == claims  # accounting equation across the whole ledger


async def test_callback_is_idempotent_and_rejects_contradiction(client, merchant):
    p = await settled_payment(client, merchant, 30000)
    again = await client.post(f"/v1/payments/{p['id']}/callback", params={"success": "true"})
    assert again.status_code == 200
    assert (await client.get("/v1/merchant/balance", headers=merchant["headers"])).json()["balance_cents"] == 30000 - 900
    contradict = await client.post(f"/v1/payments/{p['id']}/callback", params={"success": "false"})
    assert contradict.status_code == 409
    entries = (await client.get(f"/v1/payments/{p['id']}/ledger", headers=merchant["headers"])).json()
    assert len(entries) == 2


async def test_failed_callback_then_capture_rejected(client, merchant):
    pid = (await new_payment(client, merchant, 15000)).json()["id"]
    assert (await client.post(f"/v1/payments/{pid}/callback", params={"success": "false"})).json()["status"] == "failed"
    assert (await client.post(f"/v1/payments/{pid}/callback", params={"success": "true"})).status_code == 409
    assert (await client.get("/v1/merchant/balance", headers=merchant["headers"])).json()["balance_cents"] == 0


async def test_cancel(client, merchant):
    pid = (await new_payment(client, merchant, 15000)).json()["id"]
    r = await client.post(f"/v1/payments/{pid}/callback", params={"cancelled": "true"})
    assert r.json()["status"] == "cancelled"


# ---------- refunds ----------

async def test_partial_then_full_refund(client, merchant):
    p = await settled_payment(client, merchant, 50000)      # fee 1400, net 48600
    h = merchant["headers"]
    r1 = await client.post(f"/v1/payments/{p['id']}/refund", params={"amount": 10000}, headers=h)
    assert r1.status_code == 200 and r1.json()["status"] == "partially_refunded"
    r2 = await client.post(f"/v1/payments/{p['id']}/refund", params={"amount": 5000}, headers=h)
    assert r2.status_code == 200 and r2.json()["refunded_amount_cents"] == 15000
    over = await client.post(f"/v1/payments/{p['id']}/refund", params={"amount": 40000}, headers=h)
    assert over.status_code == 409
    assert (await client.get("/v1/merchant/balance", headers=h)).json()["balance_cents"] == 48600 - 15000
    assets, claims = await _balances()
    assert assets == claims


async def test_refund_limited_by_merchant_balance(client, merchant):
    p = await settled_payment(client, merchant, 50000)
    r = await client.post(f"/v1/payments/{p['id']}/refund", headers=merchant["headers"])  # full 50000 > balance 48600
    assert r.status_code == 409 and "balance" in r.json()["detail"].lower()


async def test_cannot_refund_unsettled(client, merchant):
    pid = (await new_payment(client, merchant)).json()["id"]
    assert (await client.post(f"/v1/payments/{pid}/refund", headers=merchant["headers"])).status_code == 409


# ---------- checkout page ----------

async def test_checkout_page_escapes_content(client, merchant):
    r = await new_payment(client, merchant, 15000, description="<script>alert(1)</script>", merchant_reference="</script><b>")
    pid = r.json()["id"]
    page = (await client.get(f"/v1/payments/{pid}/checkout")).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


async def test_sandbox_disabled_in_production(client, merchant, settings, monkeypatch):
    pid = (await new_payment(client, merchant)).json()["id"]
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert (await client.get(f"/v1/payments/{pid}/checkout")).status_code == 403
    assert (await client.post(f"/v1/payments/{pid}/callback")).status_code == 403


# ---------- webhooks ----------

class FakeResponse:
    def __init__(self, code):
        self.status_code, self.text = code, "ok"


class FakeClient:
    def __init__(self, codes):
        self.codes, self.calls = list(codes), []

    async def post(self, url, content, headers):
        self.calls.append((url, content, headers))
        return FakeResponse(self.codes.pop(0))


async def test_webhook_crud_and_secret_visibility(client, merchant):
    h = merchant["headers"]
    created = await client.post("/v1/webhooks", json={"url": "http://localhost:9/h", "events": ["payment.captured"]}, headers=h)
    assert created.status_code == 201 and created.json()["secret"]
    listed = (await client.get("/v1/webhooks", headers=h)).json()
    assert listed and all("secret" not in w for w in listed)
    bad = await client.post("/v1/webhooks", json={"url": "http://localhost:9/h", "events": ["nope"]}, headers=h)
    assert bad.status_code == 422
    assert (await client.post("/v1/webhooks", json={"url": "ftp://x"}, headers=h)).status_code == 422
    assert (await client.delete(f"/v1/webhooks/{created.json()['id']}", headers=h)).status_code == 204


async def test_webhook_events_queued_and_delivered_with_valid_signature(client, merchant):
    h = merchant["headers"]
    wh = (await client.post("/v1/webhooks", json={"url": "http://localhost:9/h", "events": ["payment.captured"]}, headers=h)).json()
    await settled_payment(client, merchant, 19900)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(WebhookDelivery).where(WebhookDelivery.endpoint_id == uuid.UUID(wh["id"])))).scalars().all()
        assert [r.event_type for r in rows] == ["payment.captured"]   # only the subscribed event
        assert rows[0].payload["data"]["status"] == "captured"
        fake = FakeClient([500, 200])
        svc = WebhookService()
        d = await svc.deliver(db, rows[0].id, client=fake)
        assert d.status == "pending" and d.attempt_count == 1 and d.next_retry_at is not None   # retry scheduled
        d = await svc.deliver(db, rows[0].id, client=fake)
        assert d.status == "delivered" and d.attempt_count == 2
        url, body, headers = fake.calls[-1]
    assert WebhookService.verify_signature(json.loads(body), headers["X-Webhook-Signature"], wh["secret"])
    assert headers["X-Webhook-Event"] == "payment.captured"


async def test_webhook_gives_up_after_max_attempts(client, merchant, settings, monkeypatch):
    monkeypatch.setattr(settings, "WEBHOOK_MAX_ATTEMPTS", 2)
    h = merchant["headers"]
    wh = (await client.post("/v1/webhooks", json={"url": "http://localhost:9/h", "events": ["payment.failed"]}, headers=h)).json()
    pid = (await new_payment(client, merchant, 15000)).json()["id"]
    await client.post(f"/v1/payments/{pid}/callback", params={"success": "false"})
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(WebhookDelivery).where(WebhookDelivery.endpoint_id == uuid.UUID(wh["id"])))).scalars().one()
        fake = FakeClient([503, 503])
        await WebhookService().deliver(db, row.id, client=fake)
        d = await WebhookService().deliver(db, row.id, client=fake)
        assert d.status == "failed" and d.next_retry_at is None
