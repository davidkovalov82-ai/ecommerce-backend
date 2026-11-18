import datetime
import importlib
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.orders.dependencies import get_payments_client
from app.modules.orders.payments_client import (
    HttpPaymentsClient,
    PaymentGatewayError,
    PaymentsNotConfigured,
    RemoteOrderAlreadyPaid,
    RemoteOrderAmountInvalid,
    RemotePaymentConflict,
)
from tests.payments.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
)

client = TestClient(app)


def test_commerce_has_no_stripe_secrets_or_dependency() -> None:
    """Commerce не притежава Stripe ключове и не зависи от stripe SDK."""
    assert not hasattr(settings, "stripe_secret_key")
    assert not hasattr(settings, "stripe_webhook_secret")
    assert not hasattr(settings, "stripe_mode")

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("stripe")


def test_internal_payments_token_not_leaked_in_http_errors(
    db_session: Session,
) -> None:
    """Вътрешният токен към Payments никога не се появява в грешките към клиента."""
    secret_token = "super-secret-internal-token-do-not-leak"
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "Leak Test", price_minor=1000)
    order = _create_order(db_session, user.id, product)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout while calling payments", request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client(base_url="http://payments.local", transport=transport)
    fake_payments_client = HttpPaymentsClient(
        base_url="http://payments.local",
        internal_token=secret_token,
    )

    class _PatchedClient(HttpPaymentsClient):
        def create_checkout_session(self, **kwargs):  # type: ignore[override]
            try:
                with real_client as c:
                    c.post(
                        "/internal/payments/checkout-sessions",
                        json={},
                        headers=self._headers(),
                    )
            except httpx.HTTPError as exc:
                raise PaymentGatewayError(
                    f"Payments failed with {secret_token}: {exc}"
                ) from exc

    app.dependency_overrides[get_payments_client] = lambda: _PatchedClient(
        base_url=fake_payments_client.base_url,
        internal_token=secret_token,
    )
    try:
        resp = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 502
        assert secret_token not in resp.text
        assert resp.json()["detail"]["code"] == "PAYMENT_GATEWAY_ERROR"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)


def test_http_payments_client_missing_token_raises_not_configured() -> None:
    """Липса на PAYMNENTS_INTERNAL_AUTH_TOKEN води до PaymentsNotConfigured."""
    hp = HttpPaymentsClient(base_url="http://payments.local", internal_token="")
    with pytest.raises(PaymentsNotConfigured):
        hp.create_checkout_session(
            order_id=1,
            attempt_key="order_1_attempt_1",
            amount_minor=1000,
            currency="EUR",
            line_items=[],
            order_expires_at=None,
        )


def test_http_payments_client_maps_headers_and_errors() -> None:
    """HttpPaymentsClient изпраща X-Internal-Token и превежда грешките коректно."""
    seen_headers: dict[str, str] = {}

    def ok_handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "payment_attempt_id": 42,
                "session_id": "cs_http_42",
                "checkout_url": "https://checkout.stripe.com/pay/cs_http_42",
                "status": "open",
            },
        )

    transport = httpx.MockTransport(ok_handler)

    with patch(
        "app.modules.orders.payments_client.httpx.Client",
        return_value=httpx.Client(
            base_url="http://payments.local", transport=transport
        ),
    ):
        hp = HttpPaymentsClient(
            base_url="http://payments.local",
            internal_token="token-xyz",
        )
        res = hp.create_checkout_session(
            order_id=5,
            attempt_key="order_5_attempt_1",
            amount_minor=2500,
            currency="EUR",
            line_items=[{"name": "Item", "unit_amount": 2500, "quantity": 1}],
            order_expires_at=datetime.datetime.now(datetime.UTC),
        )
        assert res.payment_attempt_id == 42
        assert res.session_id == "cs_http_42"
        assert seen_headers.get("x-internal-token") == "token-xyz"

    for status_code, body, expected_exc in [
        (
            400,
            {"detail": {"code": "INVALID_ORDER_AMOUNT", "message": "too low"}},
            RemoteOrderAmountInvalid,
        ),
        (
            409,
            {"detail": {"code": "ORDER_ALREADY_PAID", "message": "paid"}},
            RemoteOrderAlreadyPaid,
        ),
        (
            409,
            {
                "detail": {
                    "code": "PAYMENT_IN_PROGRESS",
                    "current_status": "processing",
                    "message": "busy",
                }
            },
            RemotePaymentConflict,
        ),
        (
            503,
            {"detail": {"code": "STRIPE_NOT_CONFIGURED", "message": "no key"}},
            PaymentsNotConfigured,
        ),
        (500, {"detail": "internal"}, PaymentGatewayError),
    ]:
        err_transport = httpx.MockTransport(
            lambda _req, sc=status_code, payload=body: httpx.Response(sc, json=payload)
        )
        with patch(
            "app.modules.orders.payments_client.httpx.Client",
            return_value=httpx.Client(
                base_url="http://payments.local", transport=err_transport
            ),
        ):
            hp_err = HttpPaymentsClient(
                base_url="http://payments.local",
                internal_token="token-xyz",
            )
            with pytest.raises(expected_exc):
                hp_err.cancel_order_payment(order_id=5)
