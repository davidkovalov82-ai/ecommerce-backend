"""Помощни функции за тестовете на Payments сервиса."""

import hashlib
import hmac
import os
import time
from typing import Any

os.environ.setdefault(
    "PAYMENTS_INTERNAL_AUTH_TOKEN", "test-internal-payments-token-32b!"
)

from app.config import settings

TEST_WEBHOOK_SECRET = "whsec_test_secret_stripe_payments_32_bytes_ok"
TEST_INTERNAL_TOKEN = settings.internal_auth_token


def build_internal_checkout_payload(
    order_id: int,
    *,
    attempt_key: str | None = None,
    amount_minor: int = 5000,
    order_expires_at: str | None = None,
) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "attempt_key": attempt_key or f"order_{order_id}_attempt_1",
        "amount_minor": amount_minor,
        "currency": "EUR",
        "line_items": [
            {
                "name": "Test Item",
                "unit_amount": amount_minor,
                "quantity": 1,
                "currency": "eur",
            }
        ],
        "order_expires_at": order_expires_at,
    }


def generate_stripe_signature(
    payload_bytes: bytes,
    secret: str = TEST_WEBHOOK_SECRET,
    timestamp: int | None = None,
) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode() + payload_bytes
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"
