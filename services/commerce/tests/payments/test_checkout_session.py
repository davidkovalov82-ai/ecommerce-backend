import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.main import app
from app.modules.orders.dependencies import get_stripe_client
from app.modules.orders.models import Order, StripeCheckoutSession
from app.modules.orders.stripe_client import StripeClient
from tests.payments.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
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

    # Несъществуваща поръчка
    resp = client.post(
        "/orders/99999/checkout-session",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "ORDER_NOT_FOUND"

    # Чужда поръчка
    resp_foreign = client.post(
        f"/orders/{order_a.id}/checkout-session",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp_foreign.status_code == 404
    assert resp_foreign.json()["detail"]["code"] == "ORDER_NOT_FOUND"


def test_checkout_session_invalid_order_status(db_session: Session) -> None:
    """Платена или отменена поръчка връща 409 ORDER_STATUS_CONFLICT."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=1000)

    paid_order = _create_order(db_session, user.id, product, status="paid")
    resp_paid = client.post(
        f"/orders/{paid_order.id}/checkout-session",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_paid.status_code == 409
    assert resp_paid.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"

    cancelled_order = _create_order(db_session, user.id, product, status="cancelled")
    resp_cancelled = client.post(
        f"/orders/{cancelled_order.id}/checkout-session",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_cancelled.status_code == 409
    assert resp_cancelled.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"


def test_checkout_session_minimum_and_maximum_amount(db_session: Session) -> None:
    """Сума под 50 цента или над 99 999 999 цента връща 400 INVALID_ORDER_AMOUNT."""
    user, token = _create_user_and_token(db_session)

    # 40 цента < 50 цента
    product_cheap = _create_product(db_session, "Cheap", price_minor=20, stock=5)
    order_cheap = _create_order(db_session, user.id, product_cheap, quantity=2)
    resp = client.post(
        f"/orders/{order_cheap.id}/checkout-session",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_ORDER_AMOUNT"

    # Прекалено голяма сума > 99 999 999
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


def test_checkout_session_stripe_not_configured(db_session: Session) -> None:
    """Липса на STRIPE_SECRET_KEY връща 503 STRIPE_NOT_CONFIGURED."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=1500)
    order = _create_order(db_session, user.id, product)

    with patch.object(settings, "stripe_secret_key", None):
        resp = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "STRIPE_NOT_CONFIGURED"


def test_checkout_session_success_and_idempotency(db_session: Session) -> None:
    """Успешно създаване връща checkout_url, а повторно извикване връща същата сесия."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2500)
    order = _create_order(db_session, user.id, product, quantity=2)

    mock_session = MagicMock()
    mock_session.id = "cs_test_success_123"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_test_success_123"

    mock_client = MagicMock(spec=StripeClient)
    mock_client.create_checkout_session.return_value = mock_session

    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        # Първо извикване -> създаване през Stripe
        resp1 = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["checkout_url"] == mock_session.url
        assert data1["session_id"] == mock_session.id
        assert mock_client.create_checkout_session.call_count == 1

        # Проверка в базата данни за запис в stripe_checkout_sessions
        session_rec = db_session.execute(
            text(
                "SELECT status, amount_minor, currency, stripe_session_id "
                "FROM stripe_checkout_sessions WHERE order_id = :oid"
            ),
            {"oid": order.id},
        ).fetchone()
        assert session_rec is not None
        assert session_rec[0] == "open"
        assert session_rec[1] == 5000
        assert session_rec[2] == "EUR"
        assert session_rec[3] == "cs_test_success_123"

        # Второ извикване за същата поръчка -> връща съществуващата сесия БЕЗ нов Stripe call
        resp2 = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["checkout_url"] == mock_session.url
        assert data2["session_id"] == mock_session.id
        assert mock_client.create_checkout_session.call_count == 1
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_checkout_session_during_cancelling_returns_409(db_session: Session) -> None:
    """Критерий: нов checkout по време на cancelling връща 409 и не заобикаля защитата."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000)
    order = _create_order(db_session, user.id, product)

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="cancelling",
        amount_minor=4000,
        currency="EUR",
        stripe_session_id="cs_cancelling_123",
        checkout_url="https://checkout.stripe.com/c/pay/cs_cancelling_123",
        idempotency_key="idemp_cancelling_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    resp = client.post(
        f"/orders/{order.id}/checkout-session",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"


def test_checkout_session_delayed_response_does_not_reopen_completed_or_cancelled_session(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Критерий: Закъснял отговор от Stripe не трябва да върне completed или expired сесия в open."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000)
    order = _create_order(db_session, user.id, product)

    mock_session = MagicMock()
    mock_session.id = "cs_delayed_123"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_delayed_123"

    # Симулираме, че докато тече повикването към Stripe, поръчката се анулира
    def _simulate_concurrent_cancel(*args: Any, **kwargs: Any) -> Any:
        with session_factory() as s:
            o = s.get(Order, order.id)
            if o:
                o.status = "cancelled"
            sess = (
                s.query(StripeCheckoutSession)
                .filter(StripeCheckoutSession.order_id == order.id)
                .first()
            )
            if sess:
                sess.status = "expired"
            s.commit()
        return mock_session

    mock_client = MagicMock(spec=StripeClient)
    mock_client.create_checkout_session.side_effect = _simulate_concurrent_cancel

    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409

        # Проверка: статусът в базата си остава expired, а НЕ open!
        db_session.refresh(order)
        sess_db = (
            db_session.query(StripeCheckoutSession)
            .filter(StripeCheckoutSession.order_id == order.id)
            .first()
        )
        assert sess_db is not None
        assert sess_db.status == "expired"
        assert order.status == "cancelled"
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_checkout_session_parallel_requests(db_session: Session) -> None:
    """Две паралелни заявки връщат същия сесиен линк без дублирани записи в базата."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000)
    order = _create_order(db_session, user.id, product, quantity=2)

    mock_session = MagicMock()
    mock_session.id = "cs_test_concurrent_999"
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_test_concurrent_999"

    mock_client = MagicMock(spec=StripeClient)
    mock_client.create_checkout_session.return_value = mock_session

    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:

        def _call_session() -> tuple[int, dict[str, Any]]:
            thread_client = TestClient(app)
            r = thread_client.post(
                f"/orders/{order.id}/checkout-session",
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code, r.json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(_call_session)
            f2 = executor.submit(_call_session)
            res1_code, res1_data = f1.result()
            res2_code, res2_data = f2.result()

        assert res1_code == 200
        assert res2_code == 200
        assert res1_data["checkout_url"] == res2_data["checkout_url"]
        assert res1_data["session_id"] == res2_data["session_id"]

        count = db_session.execute(
            text("SELECT count(*) FROM stripe_checkout_sessions WHERE order_id = :oid"),
            {"oid": order.id},
        ).scalar()
        assert count == 1
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_checkout_session_network_error_returns_502(db_session: Session) -> None:
    """Мрежова грешка при връзка със Stripe връща 502 STRIPE_ERROR."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000)
    order = _create_order(db_session, user.id, product)

    mock_client = MagicMock(spec=StripeClient)
    mock_client.create_checkout_session.side_effect = ConnectionError("Stripe API down")

    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 502
        assert resp.json()["detail"]["code"] == "STRIPE_ERROR"
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_simulator_endpoint_removed_cannot_change_status(
    db_session: Session,
) -> None:
    """Критерий: Публичният POST /orders/{id}/payments е премахнат и не може да обяви поръчка за платена."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock = 8
    initial_stock = product.stock

    # Опит за извикване на стария симулаторен endpoint с test_success
    resp = client.post(
        f"/orders/{order.id}/payments",
        json={"payment_method": "test_success"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    # Тъй като endpoint-ът е премахнат, FastAPI връща 404 Not Found (или 405)
    assert resp.status_code in (404, 405)

    # Проверка в базата: поръчката ЗАДЪЛЖИТЕЛНО остава в pending_payment
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == "pending_payment"
    assert product.stock == initial_stock
