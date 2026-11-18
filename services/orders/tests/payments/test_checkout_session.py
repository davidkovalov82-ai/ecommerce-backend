import datetime
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.modules.accounts.repository import UserRepository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.models import Product
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.dependencies import get_payments_client
from app.modules.orders.models import Order
from app.modules.orders.payments_client import (
    CancelPaymentResult,
    CheckoutSessionResult,
    PaymentGatewayError,
    PaymentsClient,
    PaymentsNotConfigured,
    RemoteOrderAlreadyPaid,
    RemotePaymentConflict,
)
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from tests.payments.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
    make_payment_event,
)

client = TestClient(app)


def test_checkout_session_auth_required(db_session: Session) -> None:
    """Анонимен достъп връща 401."""
    response = client.post("/orders/1/checkout-session")
    assert response.status_code == 401


def test_checkout_session_foreign_and_not_found(db_session: Session) -> None:
    """Чужда или липсваща поръчка връща 404."""
    user_a, token_a = _create_user_and_token(db_session, "user_a@example.com")
    _user_b, token_b = _create_user_and_token(db_session, "user_b@example.com")
    product = _create_product(db_session, "P1", price_minor=1000)
    order_a = _create_order(db_session, user_a.id, product)

    resp = client.post(
        "/orders/99999/checkout-session",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "ORDER_NOT_FOUND"

    resp_foreign = client.post(
        f"/orders/{order_a.id}/checkout-session",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp_foreign.status_code == 404
    assert resp_foreign.json()["detail"]["code"] == "ORDER_NOT_FOUND"


def test_checkout_session_invalid_order_status(db_session: Session) -> None:
    """Платена, отменена, изтекла или обработваща се поръчка връща 409 ORDER_STATUS_CONFLICT."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=1000, stock=20)

    for invalid_status in ("paid", "cancelled", "expired", "payment_processing"):
        bad_order = _create_order(db_session, user.id, product, status=invalid_status)
        resp = client.post(
            f"/orders/{bad_order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"


def test_checkout_session_minimum_and_maximum_amount(db_session: Session) -> None:
    """Сума под 50 цента или над 99 999 999 цента връща 400 INVALID_ORDER_AMOUNT."""
    user, token = _create_user_and_token(db_session)

    product_cheap = _create_product(db_session, "Cheap", price_minor=20, stock=5)
    order_cheap = _create_order(db_session, user.id, product_cheap, quantity=2)
    resp = client.post(
        f"/orders/{order_cheap.id}/checkout-session",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_ORDER_AMOUNT"

    product_expensive = _create_product(
        db_session, "Expensive", price_minor=100_000_000, stock=5
    )
    order_expensive = _create_order(db_session, user.id, product_expensive, quantity=1)
    resp_exp = client.post(
        f"/orders/{order_expensive.id}/checkout-session",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_exp.status_code == 400
    assert resp_exp.json()["detail"]["code"] == "INVALID_ORDER_AMOUNT"


def test_checkout_session_payments_not_configured(db_session: Session) -> None:
    """Липсващ вътрешен токен или Stripe конфигурация в Payments връща 503."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "NoKeyItem", price_minor=1500)
    order = _create_order(db_session, user.id, product)

    mock_client = MagicMock(spec=PaymentsClient)
    mock_client.create_checkout_session.side_effect = PaymentsNotConfigured(
        "missing internal auth"
    )
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "PAYMENTS_NOT_CONFIGURED"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)


def test_checkout_session_passes_authoritative_order_data_and_rotates_on_failure(
    db_session: Session,
) -> None:
    """Commerce подава сума/позиции/attempt_key от БД и сменя attempt_key само след терминален неуспех."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "Authoritative Item", price_minor=2500)
    expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=20)
    order = _create_order(
        db_session, user.id, product, quantity=2, expires_at=expires_at
    )

    mock_client = MagicMock(spec=PaymentsClient)
    mock_client.create_checkout_session.return_value = CheckoutSessionResult(
        payment_attempt_id=10,
        session_id="cs_auth_1",
        checkout_url="https://checkout.stripe.com/pay/cs_auth_1",
        status="open",
    )
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        r1 = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        r2 = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json() == {
            "checkout_url": "https://checkout.stripe.com/pay/cs_auth_1",
            "session_id": "cs_auth_1",
        }

        assert mock_client.create_checkout_session.call_count == 2
        kwargs = mock_client.create_checkout_session.call_args.kwargs
        assert kwargs["order_id"] == order.id
        assert kwargs["attempt_key"] == f"order_{order.id}_attempt_1"
        assert kwargs["amount_minor"] == 5000
        assert kwargs["currency"] == "EUR"
        assert kwargs["line_items"] == [
            {
                "name": "Authoritative Item",
                "unit_amount": 2500,
                "quantity": 2,
                "currency": "eur",
            }
        ]

        # Симулираме payment.failed -> Commerce отваря attempt_2
        svc = OrderService(
            order_repository=OrderRepository(db_session),
            cart_repository=CartRepository(db_session),
            product_repository=ProductRepository(db_session),
            user_repository=UserRepository(db_session),
            session=db_session,
            payments_client=mock_client,
        )
        svc.apply_payment_event(
            make_payment_event(
                event_type="payment.failed",
                order_id=order.id,
                attempt_key=f"order_{order.id}_attempt_1",
                amount_minor=5000,
            )
        )

        mock_client.create_checkout_session.return_value = CheckoutSessionResult(
            payment_attempt_id=11,
            session_id="cs_auth_2",
            checkout_url="https://checkout.stripe.com/pay/cs_auth_2",
            status="open",
        )
        r3 = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r3.status_code == 200
        assert (
            mock_client.create_checkout_session.call_args.kwargs["attempt_key"]
            == f"order_{order.id}_attempt_2"
        )
    finally:
        app.dependency_overrides.pop(get_payments_client, None)


def test_checkout_session_parallel_requests_use_same_attempt_key_without_db_lock(
    db_session: Session,
) -> None:
    """Паралелни заявки получават същия attempt_key и не блокират БД по време на HTTP."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "ConcurrentItem", price_minor=1500)
    order = _create_order(db_session, user.id, product)

    mock_client = MagicMock(spec=PaymentsClient)
    observed_keys: list[str] = []

    def slow_payments_create(**kwargs):  # type: ignore[no-untyped-def]
        observed_keys.append(kwargs["attempt_key"])
        time.sleep(0.1)
        return CheckoutSessionResult(
            payment_attempt_id=77,
            session_id="cs_concurrent_ok",
            checkout_url="https://checkout.stripe.com/pay/cs_concurrent_ok",
            status="open",
        )

    mock_client.create_checkout_session.side_effect = slow_payments_create
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:

        def _req() -> int:
            resp = client.post(
                f"/orders/{order.id}/checkout-session",
                headers={"Authorization": f"Bearer {token}"},
            )
            return resp.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: _req(), range(2)))

        assert statuses == [200, 200]
        assert observed_keys == [
            f"order_{order.id}_attempt_1",
            f"order_{order.id}_attempt_1",
        ]
    finally:
        app.dependency_overrides.pop(get_payments_client, None)


def test_checkout_session_expired_order_coordinates_with_payments(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Просрочена поръчка съгласува прекратяване с Payments преди да освободи резерва."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "ExpiredCheck", price_minor=1200, stock=10)
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)

    # 1. Payments отказва прекратяване с RemoteOrderAlreadyPaid -> stock НЕ се връща!
    o_paid = _create_order(
        db_session, user.id, product, quantity=2, expires_at=past
    )  # stock=8
    mock_client = MagicMock(spec=PaymentsClient)
    mock_client.cancel_order_payment.side_effect = RemoteOrderAlreadyPaid("paid")
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        r_paid = client.post(
            f"/orders/{o_paid.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_paid.status_code == 409
        with session_factory() as s:
            assert s.get(Product, product.id).stock == 8  # type: ignore[union-attr]
            assert s.get(Order, o_paid.id).status == "pending_payment"  # type: ignore[union-attr]

        # 2. Payments потвърждава can_cancel -> order=expired, stock се възстановява!
        mock_client.cancel_order_payment.side_effect = None
        mock_client.cancel_order_payment.return_value = CancelPaymentResult(
            status="can_cancel", action="expired_stripe_session"
        )
        r_exp = client.post(
            f"/orders/{o_paid.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_exp.status_code == 409
        with session_factory() as s:
            assert s.get(Product, product.id).stock == 10  # type: ignore[union-attr]
            assert s.get(Order, o_paid.id).status == "expired"  # type: ignore[union-attr]
    finally:
        app.dependency_overrides.pop(get_payments_client, None)


def test_checkout_session_network_error_and_conflicts_map_status(
    db_session: Session,
) -> None:
    """Мрежова грешка с Payments връща 502; конфликт в Payments връща 409."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "ErrProd", price_minor=1500)
    order = _create_order(db_session, user.id, product)

    mock_client = MagicMock(spec=PaymentsClient)
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        mock_client.create_checkout_session.side_effect = PaymentGatewayError("down")
        r_502 = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_502.status_code == 502
        assert r_502.json()["detail"]["code"] == "PAYMENT_GATEWAY_ERROR"

        mock_client.create_checkout_session.side_effect = RemotePaymentConflict(
            "cancelling", "Cancellation in progress"
        )
        r_409 = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_409.status_code == 409
        assert r_409.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)


def test_simulator_endpoint_removed_cannot_change_status(
    db_session: Session,
) -> None:
    """Старият POST /orders/{order_id}/payments е премахнат и не променя статуса."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "OldSim", price_minor=1500)
    order = _create_order(db_session, user.id, product)

    resp = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_success"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert resp.status_code in (404, 405)

    db_session.refresh(order)
    assert order.status == "pending_payment"
