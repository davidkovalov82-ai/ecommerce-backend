from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.modules.catalog.models import Product
from app.modules.orders.dependencies import get_payments_client
from app.modules.orders.models import Order
from app.modules.orders.payments_client import (
    CancelPaymentResult,
    PaymentGatewayError,
    PaymentsClient,
    PaymentsNotConfigured,
    RemoteOrderAlreadyPaid,
    RemotePaymentConflict,
)
from tests.payments.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
)

client = TestClient(app)


def test_cancel_during_session_creation_does_not_prematurely_release_stock(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Ако в Payments се създава сесия, cancel връща 409 и НЕ освобождава stock."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "ConcurrentItem", price_minor=2000, stock=10)
    order = _create_order(db_session, user.id, product, quantity=3)  # stock=7

    mock_client = MagicMock(spec=PaymentsClient)
    mock_client.cancel_order_payment.side_effect = RemotePaymentConflict(
        current_status="initiated",
        message="Payment session creation is in progress",
    )
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    with session_factory() as s:
        assert s.get(Order, order.id).status == "pending_payment"  # type: ignore[union-attr]
        assert s.get(Product, product.id).stock == 7  # type: ignore[union-attr]


def test_cancel_order_payments_failure_does_not_restore_stock(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """При 502 от Payments поръчката остава активна и резервът НЕ се пипа."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "FailCancelItem", price_minor=2000, stock=10)
    order = _create_order(db_session, user.id, product, quantity=3)  # stock=7

    mock_client = MagicMock(spec=PaymentsClient)
    mock_client.cancel_order_payment.side_effect = PaymentGatewayError(
        "Payments unreachable"
    )
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 502
        assert resp.json()["detail"]["code"] == "PAYMENT_GATEWAY_ERROR"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    with session_factory() as s:
        assert s.get(Order, order.id).status == "pending_payment"  # type: ignore[union-attr]
        assert s.get(Product, product.id).stock == 7  # type: ignore[union-attr]


def test_cancel_order_payments_not_configured_returns_503(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Липсваща конфигурация за комуникация с Payments връща 503 и пази stock."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "NoKeyCancel", price_minor=2000, stock=10)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock=8

    mock_client = MagicMock(spec=PaymentsClient)
    mock_client.cancel_order_payment.side_effect = PaymentsNotConfigured(
        "missing internal token"
    )
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "PAYMENTS_NOT_CONFIGURED"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    with session_factory() as s:
        assert s.get(Order, order.id).status == "pending_payment"  # type: ignore[union-attr]
        assert s.get(Product, product.id).stock == 8  # type: ignore[union-attr]


def test_cancel_order_confirmed_by_payments_restores_stock(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """След потвърждение от Payments отмяната превежда поръчката в cancelled и връща stock."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "CancelOkItem", price_minor=2000, stock=10)
    order1 = _create_order(db_session, user.id, product, quantity=4)  # stock=6
    order2 = _create_order(db_session, user.id, product, quantity=1)  # stock=5

    mock_client = MagicMock(spec=PaymentsClient)
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        mock_client.cancel_order_payment.return_value = CancelPaymentResult(
            status="can_cancel", action="expired_stripe_session"
        )
        r1 = client.post(
            f"/orders/{order1.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "cancelled"

        mock_client.cancel_order_payment.return_value = CancelPaymentResult(
            status="already_expired", action="already_expired"
        )
        r2 = client.post(
            f"/orders/{order2.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "cancelled"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    with session_factory() as s:
        assert s.get(Product, product.id).stock == 10  # type: ignore[union-attr]


def test_cancel_order_race_condition_payment_wins(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Ако поръчката стане paid по време на cancel или Payments върне paid -> 409 и stock не се връща."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "RaceItem", price_minor=2000, stock=10)
    order1 = _create_order(db_session, user.id, product, quantity=2)  # stock=8
    order2 = _create_order(db_session, user.id, product, quantity=1)  # stock=7

    mock_client = MagicMock(spec=PaymentsClient)

    def mark_order_paid_during_http(**_kwargs):  # type: ignore[no-untyped-def]
        with session_factory() as s:
            o = s.get(Order, order1.id)
            assert o is not None
            o.status = "paid"
            s.commit()
        return CancelPaymentResult(status="can_cancel", action="expired")

    mock_client.cancel_order_payment.side_effect = mark_order_paid_during_http
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        r1 = client.post(
            f"/orders/{order1.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 409
        assert r1.json()["detail"]["code"] == "ORDER_ALREADY_PAID"

        mock_client.cancel_order_payment.side_effect = RemoteOrderAlreadyPaid(
            "Payment already completed"
        )
        r2 = client.post(
            f"/orders/{order2.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 409
        assert r2.json()["detail"]["code"] == "ORDER_ALREADY_PAID"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    with session_factory() as s:
        assert s.get(Product, product.id).stock == 7  # type: ignore[union-attr]
        assert s.get(Order, order1.id).status == "paid"  # type: ignore[union-attr]
        assert s.get(Order, order2.id).status == "pending_payment"  # type: ignore[union-attr]


def test_cannot_cancel_or_create_checkout_in_payment_processing(
    db_session: Session,
) -> None:
    """Поръчка в payment_processing не може да бъде отказана или пренасочена към нов checkout."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "AsyncProd", price_minor=1500, stock=5)
    order = _create_order(db_session, user.id, product, status="payment_processing")

    r_cancel = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_cancel.status_code == 409
    assert r_cancel.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"

    r_checkout = client.post(
        f"/orders/{order.id}/checkout-session",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_checkout.status_code == 409
    assert r_checkout.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"
