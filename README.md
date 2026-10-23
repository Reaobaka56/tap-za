# Payment Gateway

A developer-first payment orchestration platform built for South African payment infrastructure. It provides a unified API for cards, EFT, and pay-by-bank payments while maintaining a double-entry ledger as the financial source of truth.

> **Status: MVP / Sandbox**
>
> The current implementation uses mock payment providers. Production deployment requires appropriate payment-provider integrations, financial partnerships, regulatory compliance, security controls, and operational infrastructure.

---

## Architecture

```text
                    ┌──────────────────────┐
                    │      Merchant        │
                    │      Application     │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │     Merchant API      │
                    │       FastAPI         │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │  Payment Orchestrator │
                    │   Routing + Providers │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
       ┌────────────┐   ┌────────────┐   ┌──────────────┐
       │    Card    │   │    EFT     │   │ Pay-by-Bank  │
       │  Provider  │   │  Provider  │   │   Provider   │
       └────────────┘   └────────────┘   └──────────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │  Transaction Engine  │
                    │    State Machine     │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
       ┌────────────┐   ┌────────────┐   ┌────────────┐
       │   Ledger   │   │   Fraud    │   │  Webhooks  │
       │   Engine   │   │   Engine   │   │    HMAC    │
       └────────────┘   └────────────┘   └────────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │ Merchant Dashboard   │
                    └──────────────────────┘
```

---

## Core Features

* Unified payment API
* Payment provider orchestration
* Provider routing
* Hosted checkout
* Card payments
* EFT payments
* Pay-by-bank
* Payment state machine
* Idempotent payment creation
* Double-entry accounting ledger
* Merchant balances
* Refund processing
* Webhook delivery
* HMAC-SHA256 webhook signatures
* Fraud and risk scoring
* API-key authentication
* JWT authentication
* PostgreSQL persistence
* Redis infrastructure
* Docker-based development environment

---

## Tech Stack

| Layer             | Technology              |
| ----------------- | ----------------------- |
| Backend           | Python 3.11 + FastAPI   |
| Database          | PostgreSQL 15           |
| Database Driver   | asyncpg                 |
| ORM               | SQLAlchemy 2.0          |
| Cache / Queues    | Redis                   |
| Frontend          | Vanilla JavaScript      |
| Styling           | Tailwind-style CSS      |
| Infrastructure    | Docker + Docker Compose |
| API Documentation | OpenAPI / Swagger       |

---

## Quick Start

### Prerequisites

* Docker
* Docker Compose
* Git
* Make (optional)

### Clone the Repository

```bash
git clone <repository-url>
cd payment_gateway
```

### Start the Platform

```bash
docker-compose up --build
```

### API Documentation

```text
http://localhost:8000/docs
```

### Merchant Dashboard

```text
http://localhost:3000
```

### Configuration

Copy `.env.example` to `.env` and set real secrets. With `ENVIRONMENT=production` the API refuses to start on the default `SECRET_KEY` / `WEBHOOK_SECRET`, and the sandbox checkout and callback endpoints are disabled.

### Running the tests

```bash
cd backend
pip install -r requirements-dev.txt
pytest                      # SQLite, no services needed
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/gateway_test pytest   # same suite on PostgreSQL
```

### Services

| Service    |   Port | Description                     |
| ---------- | -----: | ------------------------------- |
| API        | `8000` | FastAPI backend                 |
| Dashboard  | `3000` | Merchant dashboard              |
| PostgreSQL | `5432` | Transaction and ledger database |
| Redis      | `6379` | Cache and job queue             |

---

## Core Concepts

### Double-Entry Ledger

The ledger is the financial source of truth.

Every financial transaction creates balanced debit and credit entries. Payment status should not be treated as the authoritative representation of money movement.

For example, a R500.00 payment with a R15.00 gateway fee:

| Account             | Type      |       Debit |      Credit |
| ------------------- | --------- | ----------: | ----------: |
| Gateway Clearing    | Asset     |     R500.00 |           — |
| Merchant Balance    | Liability |           — |     R485.00 |
| Gateway Fee Revenue | Revenue   |           — |      R15.00 |
| **Total**           |           | **R500.00** | **R500.00** |

The fundamental ledger invariant is:

```text
Total Debits = Total Credits
```

---

### Payment State Machine

```text
CREATED
   │
   ▼
PENDING
   │
   ├──────────────► FAILED
   │
   ▼
AUTHORIZED
   │
   ├──────────────► CANCELLED
   │
   ▼
CAPTURED
   │
   ▼
SETTLED
   │
   └──────────────► REFUND_PENDING
                            │
                            ▼
                         REFUNDED
```

Payment transitions are validated by the transaction engine. Invalid state transitions are rejected.

---

### Idempotency

Payment creation requests accept an `Idempotency-Key` header.

```http
POST /v1/payments
Idempotency-Key: order-123-unique
```

Repeated requests using the same idempotency key return the original payment instead of creating a duplicate transaction.

---

## API Reference

### Authentication

#### Register

```http
POST /v1/auth/register
```

```json
{
  "email": "merchant@example.com",
  "password": "********",
  "business_name": "Acme Inc"
}
```

#### Login

```http
POST /v1/auth/login
```

```json
{
  "email": "merchant@example.com",
  "password": "********"
}
```

#### Create API Key

```http
POST /v1/auth/api-keys?name=Production&environment=live
```

API keys should be treated as secrets and must not be committed to source control.

---

## Payments

### Create Payment

```http
POST /v1/payments
Idempotency-Key: order-123-unique
```

```json
{
  "amount": 19900,
  "currency": "ZAR",
  "methods": [
    "card",
    "eft",
    "pay_by_bank"
  ],
  "merchant_reference": "ORDER-123",
  "customer_email": "customer@example.com",
  "return_url": "https://merchant.co.za/success",
  "description": "Premium plan subscription"
}
```

Amounts are represented in the smallest currency unit.

```text
19900 = R199.00
```

### Response

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "amount_cents": 19900,
  "checkout_url": "http://localhost:8000/v1/payments/{id}/checkout",
  "requires_redirect": true
}
```

### Get Payment

```http
GET /v1/payments/{payment_id}
```

### List Payments

```http
GET /v1/payments?status=captured&limit=50&offset=0
```

### Provider Callback

```http
POST /v1/payments/{payment_id}/callback?success=true
```

### Refund

```http
POST /v1/payments/{payment_id}/refund?amount=5000
```

```text
5000 = R50.00
```

---

## Merchant API

### Other endpoints

```http
GET    /v1/auth/me                      # login JWT
GET    /v1/auth/api-keys                # list keys (login JWT)
DELETE /v1/auth/api-keys/{id}           # revoke a key (login JWT)
GET    /v1/payments/{id}/ledger         # ledger entries for a payment
DELETE /v1/webhooks/{id}                # deactivate an endpoint
GET    /health
```

API keys are accepted as `Authorization: Bearer pk_...` or `X-API-Key`. Keys are shown once at creation and stored hashed. Creating and revoking keys requires a login JWT, not an API key.

### Get Merchant Profile

```http
GET /v1/merchant/profile
```

### Get Merchant Balance

```http
GET /v1/merchant/balance
```

---

## Webhooks

Merchants can register webhook endpoints for payment events.

### Create Webhook

```http
POST /v1/webhooks
```

```json
{
  "url": "https://merchant.co.za/webhooks",
  "events": [
    "payment.captured",
    "payment.failed"
  ]
}
```

### List Webhooks

```http
GET /v1/webhooks
```

### Webhook Payload

```json
{
  "id": "evt_...",
  "event": "payment.captured",
  "data": {
    "id": "550e8400-...",
    "status": "captured",
    "amount_cents": 19900,
    "fee_cents": 648,
    "net_amount_cents": 19252,
    "provider_reference": "MOCK_CARD_abc123"
  },
  "created_at": "2026-08-26T12:00:00Z"
}
```

Supported events: `payment.created`, `payment.authorized`, `payment.captured`, `payment.settled`, `payment.failed`, `payment.cancelled`, `payment.refunded`, or `*`. Failed deliveries are retried with exponential backoff (5 attempts). The signing secret is returned once when the endpoint is created.

### Signature

```http
X-Webhook-Signature: t=1693046400,v1=abc123def456...
```

Webhook signatures use HMAC-SHA256.

---

## Project Structure

```text
payment_gateway/
├── docker-compose.yml
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   │
│   └── app/
│       ├── main.py
│       │
│       ├── core/
│       │   ├── config.py
│       │   ├── database.py
│       │   └── security.py
│       │
│       ├── models/
│       │   ├── merchant.py
│       │   ├── payment.py
│       │   ├── ledger.py
│       │   └── webhook.py
│       │
│       ├── services/
│       │   ├── payment_engine.py
│       │   ├── orchestrator.py
│       │   ├── ledger.py
│       │   ├── fraud.py
│       │   └── webhook.py
│       │
│       └── routers/
│           ├── auth.py
│           ├── payments.py
│           ├── merchants.py
│           └── webhooks.py
│
└── dashboard/
    └── index.html
```

---

## Provider Integrations

The MVP currently uses mock providers to simulate payment processing.

| Provider           | Methods     | Status  |
| ------------------ | ----------- | ------- |
| `mock_card`        | Card        | Mock    |
| `mock_eft`         | EFT         | Mock    |
| `mock_pay_by_bank` | Pay-by-bank | Mock    |
| Ozow               | EFT         | Planned |
| PayFast            | Card / EFT  | Planned |
| Stripe             | Card        | Planned |

Provider implementations are isolated behind the orchestration layer.

To add a provider, implement the required provider interface in:

```text
backend/app/services/orchestrator.py
```

---

## Fee Structure

Default gateway fees:

```text
Percentage: 2.5%
Fixed fee:  R1.50
```

Example for a R199.00 payment:

```text
Customer payment       R199.00
Percentage fee           R4.98
Fixed fee                R1.50
──────────────────────────────
Total gateway fee        R6.48
Merchant amount        R192.52
```

---

## Security

### Implemented

* [x] JWT authentication
* [x] API-key authentication
* [x] HMAC-SHA256 webhook signatures
* [x] Idempotency-key handling
* [x] Payment state-machine validation
* [x] Double-entry ledger
* [x] Password hashing
* [x] Environment-based configuration

### Production Requirements

* [ ] PCI DSS compliance where applicable
* [ ] KYC/KYB merchant verification
* [ ] Rate limiting
* [ ] Request signing
* [ ] Secrets management
* [ ] Audit logging
* [ ] Encryption at rest
* [ ] Provider signature verification
* [ ] Webhook retry handling
* [ ] Fraud monitoring
* [ ] Reconciliation
* [ ] Settlement controls
* [ ] Regulatory and legal review

> The MVP is intended for development and testing. It should not be used to process real customer funds until the required production controls are implemented.

---

## Roadmap

| Phase | Goal                                                     | Status      |
| ----- | -------------------------------------------------------- | ----------- |
| 1     | Merchant accounts, API keys, sandbox and hosted checkout | Complete    |
| 2     | Payment creation, redirects, webhooks and idempotency    | Complete    |
| 3     | Provider integrations and routing                        | In Progress |
| 4     | Ledger, refunds, reconciliation and settlement reporting | In Progress |
| 5     | Fraud/risk engine and operational tooling                | In Progress |
| 6     | Production security and compliance                       | Planned     |

---

## Development Principles

### API First

Merchants interact with a consistent API regardless of the underlying payment provider.

### Ledger First

Financial state is represented through accounting records rather than relying solely on payment status fields.

### Provider Agnostic

Payment providers are isolated behind a common orchestration layer.

### Idempotent

Payment operations are designed to safely handle retries.

### Auditable

Payment activity should be traceable through payment records, provider references, ledger entries and webhook events.

---

## Contributing

Contributions are welcome.

Before submitting a pull request:

1. Keep changes focused.
2. Add tests for new payment behavior.
3. Preserve ledger invariants.
4. Avoid breaking existing API contracts.
5. Never commit credentials or secrets.

---

## License

MIT License
