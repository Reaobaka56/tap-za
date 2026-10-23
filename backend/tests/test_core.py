"""Unit tests: fees, state machine, webhook signatures, SSRF guard."""
import pytest
from app.models.payment import Payment, PaymentStatus, VALID_TRANSITIONS
from app.services.payment_engine import PaymentEngine
from app.services.webhook import WebhookService, validate_webhook_url


def test_fee_matches_readme_example():
    fee, net = PaymentEngine.calculate_fees(19900)
    assert (fee, net) == (648, 19252)


@pytest.mark.parametrize("frm,to,ok", [
    (PaymentStatus.CREATED, PaymentStatus.PENDING, True),
    (PaymentStatus.PENDING, PaymentStatus.AUTHORIZED, True),
    (PaymentStatus.PENDING, PaymentStatus.CAPTURED, False),
    (PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED, True),
    (PaymentStatus.CAPTURED, PaymentStatus.SETTLED, True),
    (PaymentStatus.FAILED, PaymentStatus.CAPTURED, False),
    (PaymentStatus.REFUNDED, PaymentStatus.SETTLED, False),
    (PaymentStatus.SETTLED, PaymentStatus.PARTIALLY_REFUNDED, True),
])
def test_state_machine(frm, to, ok):
    assert Payment(status=frm.value).can_transition_to(to) is ok


def test_terminal_states_have_no_exits():
    for terminal in (PaymentStatus.FAILED, PaymentStatus.CANCELLED, PaymentStatus.EXPIRED, PaymentStatus.REFUNDED):
        assert terminal not in VALID_TRANSITIONS


def test_webhook_signature_roundtrip():
    payload = {"id": "evt_1", "event": "payment.captured", "data": {"amount_cents": 100}}
    header = WebhookService.generate_signature(payload, "whsec_x")
    assert WebhookService.verify_signature(payload, header, "whsec_x")
    assert not WebhookService.verify_signature(payload, header, "whsec_other")
    assert not WebhookService.verify_signature({**payload, "event": "payment.failed"}, header, "whsec_x")


def test_webhook_signature_rejects_stale_timestamp():
    payload = {"a": 1}
    header = WebhookService.generate_signature(payload, "s", timestamp="1000000000")
    assert not WebhookService.verify_signature(payload, header, "s")
    assert WebhookService.verify_signature(payload, header, "s", tolerance_seconds=None)


def test_ssrf_guard_in_production(settings, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    for bad in ("http://example.com/hook", "https://127.0.0.1/hook", "https://10.0.0.5/hook",
                "https://169.254.169.254/latest/meta-data", "ftp://example.com", "not-a-url"):
        with pytest.raises(ValueError):
            validate_webhook_url(bad)


def test_localhost_allowed_outside_production():
    validate_webhook_url("http://localhost:9000/hook")
