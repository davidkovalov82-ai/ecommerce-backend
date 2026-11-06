import datetime
import hashlib
import hmac
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.main import app
from app.modules.accounts.models import User
from app.modules.accounts.repository import UserRepository
from app.modules.accounts.tokens import create_access_token
from app.modules.cart.repository import CartRepository
from app.modules.catalog.models import Product
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.dependencies import get_stripe_client
from app.modules.orders.models import (
    Order,
    OrderItem,
    StripeCheckoutSession,
)
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from app.modules.orders.stripe_client import StripeClient
from tests.conftest import TestingSessionLocal

client = TestClient(app)

TEST_WEBHOOK_SECRET = "whsec_test_secret_stripe_commerce_32_bytes_ok"


def _create_user_and_token(
    db_session: Session,
    email: str = "stripe_test_user@example.com",
    role: str = "customer",
) -> tuple[User, str]:
    """Създава тестов потребител и валиден Bearer токен."""
    user = User(
        email=email,
        password_hash="argon2_test_hash",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    token = create_access_token(user_id=user.id, secret=settings.jwt_secret)
    return user, token


def _create_product(
    db_session: Session,
    title: str = "Stripe Test Product",
    price_minor: int = 2500,
    stock: int = 10,
    active: bool = True,
) -> Product:
    """Създава тестов продукт в каталога."""
    product = Product(
        title=title,
        price_minor=price_minor,
        currency="EUR",
        stock=stock,
        active=active,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def _create_order(
    db_session: Session,
    user_id: int,
    product: Product,
    quantity: int = 2,
    status: str = "pending_payment",
) -> Order:
    """Директно създава поръчка със запазена позиция и резервиран stock."""
    total_minor = product.price_minor * quantity
    order = Order(
        user_id=user_id,
        status=status,
        currency="EUR",
        total_minor=total_minor,
        idempotency_key=str(uuid.uuid4()),
    )
    db_session.add(order)
    db_session.flush()

    order_item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        product_title=product.title,
        quantity=quantity,
        unit_price_minor=product.price_minor,
        subtotal_minor=total_minor,
    )
    db_session.add(order_item)
    product.stock -= quantity
    db_session.commit()
    db_session.refresh(order)
    return order


def _generate_stripe_signature(
    payload_bytes: bytes,
    secret: str = TEST_WEBHOOK_SECRET,
    timestamp: int | None = None,
) -> str:
    """Генерира валиден Stripe-Signature хедър чрез HMAC SHA256."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode() + payload_bytes
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


# ============================================================================
# 1. POST /orders/{order_id}/checkout-session
# ============================================================================


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
        with TestingSessionLocal() as s:
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


# ============================================================================
# 2. POST /webhooks/stripe
# ============================================================================


def test_webhook_no_jwt_required(db_session: Session) -> None:
    """Уебхукът не изисква JWT автентикация."""
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post("/webhooks/stripe", content=b"")
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "INVALID_STRIPE_SIGNATURE"


def test_webhook_invalid_signature_returns_400(db_session: Session) -> None:
    """Подправен или грешен подпис връща 400 без промени в базата."""
    payload = json.dumps(
        {"id": "evt_123", "type": "checkout.session.completed"}
    ).encode()
    bad_sig = "t=1234567,v1=bad_hex_signature"

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": bad_sig},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "INVALID_STRIPE_SIGNATURE"


def test_webhook_stripe_not_configured_returns_503(db_session: Session) -> None:
    """Липса на STRIPE_WEBHOOK_SECRET връща 503."""
    payload = b'{"id": "evt_123"}'
    with patch.object(settings, "stripe_webhook_secret", None):
        resp = client.post(
            "/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": "t=1,v1=dummy"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "STRIPE_NOT_CONFIGURED"


def test_webhook_livemode_ignored_in_sandbox(db_session: Session) -> None:
    """Критерий: В sandbox режим уебхуци с livemode=True се игнорират и не маркират поръчката като платена."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=3000)
    order = _create_order(db_session, user.id, product, quantity=1)

    event_payload = {
        "id": "evt_live_123",
        "type": "checkout.session.completed",
        "livemode": True,
        "data": {
            "object": {
                "id": "cs_live_123",
                "payment_status": "paid",
                "amount_total": 3000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {"order_id": str(order.id), "attempt_id": "1"},
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    db_session.refresh(order)
    assert order.status == "pending_payment"


def test_webhook_checkout_session_completed_success_and_stock_preserved(
    db_session: Session,
) -> None:
    """Критерий: Успешен webhook маркира paid, без повторно сваляне на stock."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=3000, stock=8)
    order = _create_order(db_session, user.id, product, quantity=2)  # total: 6000
    initial_stock = product.stock  # 8 - 2 = 6

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=6000,
        currency="EUR",
        stripe_session_id="cs_completed_123",
        checkout_url="https://checkout.stripe.com/pay/cs_completed_123",
        idempotency_key="idemp_123",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_order_paid_123",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_completed_123",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 6000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    db_session.refresh(order)
    db_session.refresh(product)
    db_session.refresh(session_rec)

    assert order.status == "paid"
    # Складовата наличност НЕ е намалявана повторно!
    assert product.stock == initial_stock
    assert session_rec.status == "completed"

    event_exists = db_session.execute(
        text(
            "SELECT count(*) FROM stripe_processed_events "
            "WHERE stripe_event_id = 'evt_order_paid_123'"
        )
    ).scalar()
    assert event_exists == 1


def test_webhook_before_stripe_create_response_links_attempt(
    db_session: Session,
) -> None:
    """Критерий: Ако webhook пристигне преди завършване на create_checkout_session, сесията се свързва чрез записания опит."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000, stock=5)
    order = _create_order(db_session, user.id, product, quantity=1)

    # Записваме опит в статус initiated БЕЗ stripe_session_id (Stripe create още не е върнал отговор в DB)
    attempt = StripeCheckoutSession(
        order_id=order.id,
        status="initiated",
        amount_minor=2000,
        currency="EUR",
        stripe_session_id=None,
        checkout_url=None,
        idempotency_key="idemp_early_1",
    )
    db_session.add(attempt)
    db_session.commit()

    # Webhook пристига преди create отговора!
    event_payload = {
        "id": "evt_early_arrival_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_early_stripe_id_999",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 2000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(attempt.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    db_session.refresh(order)
    db_session.refresh(attempt)
    assert order.status == "paid"
    # Сесията е коректно свързана с полученото Stripe ID и е маркирана като completed
    assert attempt.stripe_session_id == "cs_early_stripe_id_999"
    assert attempt.status == "completed"


def test_webhook_mismatched_attempt_id(db_session: Session) -> None:
    """Критерий: Не е достатъчно да съвпадат само order_id и сумата; при несъвпадащ attempt_id поръчката не се маркира paid."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000)
    order = _create_order(db_session, user.id, product, quantity=1)

    # Подаваме несъществуващ attempt_id
    event_payload = {
        "id": "evt_bad_attempt_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_bad_attempt_1",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 2000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": "999999",
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "mismatched"

    db_session.refresh(order)
    assert order.status == "pending_payment"


def test_webhook_concurrent_deliveries_no_500_or_duplicate_mutation(
    db_session: Session,
) -> None:
    """Критерий: Две едновременни доставки на едно event ID не дават 500 и не прилагат промяната два пъти."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000, stock=10)
    order = _create_order(db_session, user.id, product, quantity=1)

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=2000,
        currency="EUR",
        stripe_session_id="cs_concurrent_123",
        checkout_url="https://checkout.stripe.com/pay/cs_concurrent_123",
        idempotency_key="idemp_conc_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_concurrent_delivery_123",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_concurrent_123",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 2000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    def _send_webhook() -> tuple[int, dict[str, Any]]:
        thread_client = TestClient(app)
        with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
            r = thread_client.post(
                "/webhooks/stripe",
                content=payload_bytes,
                headers={"Stripe-Signature": sig},
            )
            return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_send_webhook)
        f2 = executor.submit(_send_webhook)
        code1, data1 = f1.result()
        code2, data2 = f2.result()

    # И двете заявки връщат 200 OK (нито една не гърми с 500)
    assert code1 == 200
    assert code2 == 200
    statuses = [data1["status"], data2["status"]]
    assert "success" in statuses
    assert "already_processed" in statuses or statuses == ["success", "success"]

    # В базата има точно 1 запис за събитието
    count = db_session.execute(
        text(
            "SELECT count(*) FROM stripe_processed_events "
            "WHERE stripe_event_id = 'evt_concurrent_delivery_123'"
        )
    ).scalar()
    assert count == 1


def test_webhook_payment_unpaid_leaves_order_pending(db_session: Session) -> None:
    """Неуспешно плащане (напр. отхвърлена карта) оставя поръчката pending_payment."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000)
    order = _create_order(db_session, user.id, product, quantity=1)

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=2000,
        currency="EUR",
        stripe_session_id="cs_unpaid_456",
        checkout_url="https://checkout.stripe.com/pay/cs_unpaid_456",
        idempotency_key="idemp_unpaid_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_order_unpaid_456",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_unpaid_456",
                "livemode": False,
                "payment_status": "unpaid",
                "amount_total": 2000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "processing"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    assert order.status == "payment_processing"
    assert session_rec.status == "processing"


def test_webhook_mismatched_amount_or_currency(db_session: Session) -> None:
    """Несъответствие в сума или валута не променя поръчката."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=3000)
    order = _create_order(db_session, user.id, product, quantity=1)  # 3000 EUR

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=3000,
        currency="EUR",
        stripe_session_id="cs_mismatch_123",
        checkout_url="https://checkout.stripe.com/pay/cs_mismatch_123",
        idempotency_key="idemp_mismatch_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    # 1. Грешна сума (1000 вместо 3000)
    event_bad_amount = {
        "id": "evt_bad_amt_789",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_mismatch_123",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 1000,
                "currency": "eur",
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    p_bytes = json.dumps(event_bad_amount).encode()
    sig = _generate_stripe_signature(p_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=p_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "mismatched"

    db_session.refresh(order)
    assert order.status == "pending_payment"

    # 2. Грешна валута (USD вместо EUR)
    event_bad_curr = {
        "id": "evt_bad_curr_789",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_mismatch_123",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 3000,
                "currency": "usd",
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    p_bytes_curr = json.dumps(event_bad_curr).encode()
    sig_curr = _generate_stripe_signature(p_bytes_curr, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp_c = client.post(
            "/webhooks/stripe",
            content=p_bytes_curr,
            headers={"Stripe-Signature": sig_curr},
        )
        assert resp_c.status_code == 200
        assert resp_c.json()["status"] == "mismatched"

    db_session.refresh(order)
    assert order.status == "pending_payment"


def test_webhook_non_existent_order_and_other_events(db_session: Session) -> None:
    """Несъществуваща поръчка и други събития се обработват безопасно."""
    evt_ghost = {
        "id": "evt_ghost_999",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_ghost_999",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 5000,
                "currency": "eur",
                "metadata": {"order_id": "888888", "attempt_id": "1"},
            }
        },
    }
    p_ghost = json.dumps(evt_ghost).encode()
    sig_ghost = _generate_stripe_signature(p_ghost, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=p_ghost,
            headers={"Stripe-Signature": sig_ghost},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "order_not_found"

    # Друг тип събитие (напр. charge.updated)
    evt_other = {
        "id": "evt_other_555",
        "type": "charge.updated",
        "livemode": False,
        "data": {"object": {"id": "ch_123"}},
    }
    p_other = json.dumps(evt_other).encode()
    sig_other = _generate_stripe_signature(p_other, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp_o = client.post(
            "/webhooks/stripe",
            content=p_other,
            headers={"Stripe-Signature": sig_other},
        )
        assert resp_o.status_code == 200
        assert resp_o.json()["status"] == "ignored"


# ============================================================================
# 3. POST /orders/{order_id}/cancel
# ============================================================================


def test_cancel_during_session_creation_does_not_prematurely_release_stock(
    db_session: Session,
) -> None:
    """Критерий: initiated без Stripe ID означава, че резултатът още не е известен.

    Отказът по време на създаване не освобождава стоката преждевременно и връща 409.
    """
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "Stock Guard", price_minor=2000, stock=5)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock = 3
    assert product.stock == 3

    # Сесия в статус initiated без Stripe ID
    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="initiated",
        amount_minor=4000,
        currency="EUR",
        stripe_session_id=None,
        checkout_url=None,
        idempotency_key="idemp_guard_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    resp = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    # Очаква се 409 Conflict, тъй като сесията се създава
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"

    # Наличността ЗАДЪЛЖИТЕЛНО остава резервирана (3, а НЕ 5)
    db_session.refresh(product)
    db_session.refresh(order)
    assert product.stock == 3
    assert order.status == "pending_payment"


def test_cancel_order_stripe_failure_does_not_treat_session_as_expired(
    db_session: Session,
) -> None:
    """Критерий: При грешка или timeout при прекратяване в Stripe сесията НЕ се приема за прекратена и stock не се връща."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000, stock=5)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock = 3

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=4000,
        currency="EUR",
        stripe_session_id="cs_fail_expire_123",
        checkout_url="https://checkout.stripe.com/pay/cs_fail_expire_123",
        idempotency_key="idemp_fail_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    mock_client = MagicMock(spec=StripeClient)
    mock_client.expire_checkout_session.side_effect = TimeoutError(
        "Stripe timeout during cancel"
    )

    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 502
        assert resp.json()["detail"]["code"] == "STRIPE_ERROR"

        # Проверка: сесията не е приета за прекратена (върната е в open), stock не е върнат
        db_session.refresh(order)
        db_session.refresh(product)
        db_session.refresh(session_rec)
        assert order.status == "pending_payment"
        assert product.stock == 3
        assert session_rec.status == "open"
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_cancel_order_missing_stripe_secret_key_does_not_cancel(
    db_session: Session,
) -> None:
    """Критерий: При липсващ Stripe ключ сесията НЕ се приема за прекратена."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000, stock=5)
    order = _create_order(db_session, user.id, product, quantity=2)

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=4000,
        currency="EUR",
        stripe_session_id="cs_no_key_123",
        checkout_url="https://checkout.stripe.com/pay/cs_no_key_123",
        idempotency_key="idemp_no_key_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    with patch.object(settings, "stripe_secret_key", None):
        resp = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "STRIPE_NOT_CONFIGURED"

        db_session.refresh(order)
        db_session.refresh(product)
        db_session.refresh(session_rec)
        assert order.status == "pending_payment"
        assert product.stock == 3
        assert session_rec.status == "open"


def test_cancel_order_expires_stripe_session_and_restores_stock(
    db_session: Session,
) -> None:
    """Отказът прекратява активната Stripe сесия и възстановява наличностите (вкл. за деактивиран продукт)."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "Deactivatable", price_minor=2000, stock=5)
    order = _create_order(db_session, user.id, product, quantity=2)
    assert product.stock == 3

    # Деактивираме продукта след checkout
    product.active = False
    db_session.commit()

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=4000,
        currency="EUR",
        stripe_session_id="cs_to_cancel_123",
        checkout_url="https://checkout.stripe.com/pay/cs_to_cancel_123",
        idempotency_key="idemp_cancel_123",
    )
    db_session.add(session_rec)
    db_session.commit()

    mock_client = MagicMock(spec=StripeClient)
    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

        mock_client.expire_checkout_session.assert_called_once_with("cs_to_cancel_123")

        db_session.refresh(order)
        db_session.refresh(product)
        db_session.refresh(session_rec)

        assert order.status == "cancelled"
        assert product.stock == 5
        assert product.active is False
        assert session_rec.status == "expired"

        # Повторно извикване -> идемпотентно 200, наличността остава 5
        resp_dup = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp_dup.status_code == 200
        db_session.refresh(product)
        assert product.stock == 5
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_cancel_order_race_condition_payment_wins(db_session: Session) -> None:
    """При състезание: ако плащането през webhook завърши, докато тече отказът, плащането побеждава."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "Race Product", price_minor=2000, stock=5)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock 3

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=4000,
        currency="EUR",
        stripe_session_id="cs_race_123",
        checkout_url="https://checkout.stripe.com/pay/cs_race_123",
        idempotency_key="idemp_race_123",
    )
    db_session.add(session_rec)
    db_session.commit()

    def _simulate_webhook_payment(session_id: str) -> None:
        with TestingSessionLocal() as s:
            o = s.get(Order, order.id)
            if o:
                o.status = "paid"
                s.commit()

    mock_client = MagicMock(spec=StripeClient)
    mock_client.expire_checkout_session.side_effect = _simulate_webhook_payment

    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "ORDER_ALREADY_PAID"

        db_session.refresh(order)
        db_session.refresh(product)
        assert order.status == "paid"
        assert product.stock == 3
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_cancel_order_already_expired_in_stripe(db_session: Session) -> None:
    """Ако сесията вече е изтекла в Stripe, отказът завършва успешно."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(
        db_session, "Expired In Stripe", price_minor=2000, stock=5
    )
    order = _create_order(db_session, user.id, product, quantity=1)

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=2000,
        currency="EUR",
        stripe_session_id="cs_already_expired_123",
        checkout_url="https://checkout.stripe.com/pay/cs_already_expired_123",
        idempotency_key="idemp_exp_123",
    )
    db_session.add(session_rec)
    db_session.commit()

    mock_client = MagicMock(spec=StripeClient)
    mock_client.expire_checkout_session.side_effect = Exception(
        "Cannot expire a session that has already expired."
    )

    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

        db_session.refresh(order)
        db_session.refresh(product)
        assert order.status == "cancelled"
        assert product.stock == 5
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


# ============================================================================
# 4. Проверка за сигурност: без изтичане на Stripe тайни
# ============================================================================


def test_stripe_keys_not_leaked_in_errors(db_session: Session) -> None:
    """Проверка, че Stripe secret keys никога не присъстват в съобщенията за грешка."""
    secret_value = "sk_test_super_secret_key_123456789"
    webhook_secret = "whsec_super_secret_webhook_987654321"

    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "Leak Test", price_minor=1000)
    order = _create_order(db_session, user.id, product)

    with (
        patch.object(settings, "stripe_secret_key", secret_value),
        patch.object(settings, "stripe_webhook_secret", webhook_secret),
    ):
        mock_client = MagicMock(spec=StripeClient)
        mock_client.create_checkout_session.side_effect = Exception(
            f"Stripe internal server issue with key {secret_value}"
        )
        app.dependency_overrides[get_stripe_client] = lambda: mock_client
        try:
            resp = client.post(
                f"/orders/{order.id}/checkout-session",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp_text = resp.text
            assert secret_value not in resp_text
            assert webhook_secret not in resp_text
        finally:
            app.dependency_overrides.pop(get_stripe_client, None)


# ============================================================================
# 5. Тестове за отложени методи (async payments), срокове (expires_at) и сверка
# ============================================================================


def test_webhook_async_payment_succeeded_marks_paid_stock_unchanged(
    db_session: Session,
) -> None:
    """Асинхронно потвърждение (async_payment_succeeded) превежда поръчката в paid, без промяна на stock."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "SEPA Product", stock=10, price_minor=5000)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock става 8
    order.status = "payment_processing"

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="processing",
        amount_minor=10000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_async_succ_123",
        checkout_url="https://checkout.stripe.com/pay/cs_async_succ_123",
        idempotency_key="idemp_async_succ_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_async_succ_1",
        "type": "checkout.session.async_payment_succeeded",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_async_succ_123",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 10000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert order.status == "paid"
    assert session_rec.status == "completed"
    assert product.stock == 8  # Наличността не се намалява повторно!


def test_webhook_async_payment_failed_resets_pending_if_not_expired(
    db_session: Session,
) -> None:
    """Неуспешно отложено плащане преди изтичане на срока връща поръчката в pending_payment (клиентът може да опита пак)."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "SEPA Fail", stock=5, price_minor=4000)
    order = _create_order(db_session, user.id, product, quantity=1)
    order.status = "payment_processing"
    order.expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        minutes=20
    )

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="processing",
        amount_minor=4000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_async_fail_123",
        checkout_url="https://checkout.stripe.com/pay/cs_async_fail_123",
        idempotency_key="idemp_async_fail_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_async_fail_1",
        "type": "checkout.session.async_payment_failed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_async_fail_123",
                "livemode": False,
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert session_rec.status == "failed"
    assert order.status == "pending_payment"
    assert product.stock == 4  # Резервацията се пази, защото срокът не е изтекъл

    # Клиентът може да създаде нов платежен линк
    mock_client = MagicMock(spec=StripeClient)
    mock_session = MagicMock()
    mock_session.id = "cs_retry_new_999"
    mock_session.url = "https://checkout.stripe.com/pay/cs_retry_new_999"
    mock_client.create_checkout_session.return_value = mock_session

    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        resp2 = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["checkout_url"] == mock_session.url
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_webhook_async_payment_failed_expires_order_if_reserve_expired(
    db_session: Session,
) -> None:
    """Неуспешно отложено плащане след изтичане на срока маркира поръчката като expired и връща stock."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "SEPA Expired", stock=5, price_minor=4000)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock става 3
    order.status = "payment_processing"
    order.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=5
    )

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="processing",
        amount_minor=8000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_async_exp_456",
        checkout_url="https://checkout.stripe.com/pay/cs_async_exp_456",
        idempotency_key="idemp_async_exp_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_async_exp_1",
        "type": "checkout.session.async_payment_failed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_async_exp_456",
                "livemode": False,
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert session_rec.status == "failed"
    assert order.status == "expired"
    assert product.stock == 5  # Наличността е възстановена точно веднъж!


def test_webhook_session_expired_event(db_session: Session) -> None:
    """Събитие checkout.session.expired маркира опита expired и освобождава стоката, ако срокът на поръчката е минал."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Timeout Prod", stock=7, price_minor=2000)
    order = _create_order(db_session, user.id, product, quantity=1)  # stock 6
    order.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=1
    )

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=2000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_sess_exp_789",
        checkout_url="https://checkout.stripe.com/pay/cs_sess_exp_789",
        idempotency_key="idemp_sess_exp_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_sess_exp_1",
        "type": "checkout.session.expired",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_sess_exp_789",
                "livemode": False,
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "expired"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert session_rec.status == "expired"
    assert order.status == "expired"
    assert product.stock == 7


def test_webhook_out_of_order_async_success_before_completed_unpaid(
    db_session: Session,
) -> None:
    """Ако async_payment_succeeded пристигне преди completed(unpaid), поръчката не се връща обратно в payment_processing."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Out of Order", stock=5, price_minor=3000)
    order = _create_order(db_session, user.id, product, quantity=1)

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=3000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_ooo_1",
        checkout_url="https://checkout.stripe.com/pay/cs_ooo_1",
        idempotency_key="idemp_ooo_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    # 1. Пристига async_payment_succeeded първо
    evt_succ = {
        "id": "evt_ooo_succ",
        "type": "checkout.session.async_payment_succeeded",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_ooo_1",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 3000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    b_succ = json.dumps(evt_succ).encode()
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=b_succ,
            headers={
                "Stripe-Signature": _generate_stripe_signature(
                    b_succ, TEST_WEBHOOK_SECRET
                )
            },
        )
        assert resp.status_code == 200

    db_session.refresh(order)
    assert order.status == "paid"

    # 2. Закъсняло събитие completed с payment_status=unpaid
    evt_compl = {
        "id": "evt_ooo_compl",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_ooo_1",
                "livemode": False,
                "payment_status": "unpaid",
                "amount_total": 3000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    b_compl = json.dumps(evt_compl).encode()
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=b_compl,
            headers={
                "Stripe-Signature": _generate_stripe_signature(
                    b_compl, TEST_WEBHOOK_SECRET
                )
            },
        )
        assert resp.status_code == 200

    db_session.refresh(order)
    assert order.status == "paid"  # Не е върната в payment_processing!


def test_webhook_discrepancy_payment_on_cancelled_or_expired_order(
    db_session: Session,
) -> None:
    """Плащане върху отказана поръчка връща status='discrepancy' и НЕ променя наличностите тихомълком."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Discrepancy Prod", stock=5, price_minor=2500)
    order = _create_order(db_session, user.id, product, quantity=1)  # stock 4
    # Потребителят е отказал поръчката: наличността е възстановена на 5
    order.status = "cancelled"
    product.stock = 5

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=2500,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_discrep_1",
        checkout_url="https://checkout.stripe.com/pay/cs_discrep_1",
        idempotency_key="idemp_discrep_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_discrep_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_discrep_1",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 2500,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "discrepancy"
        assert resp.json()["reason"] == "payment_on_cancelled_order"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert order.status == "cancelled"
    assert session_rec.status == "completed"
    assert product.stock == 5  # Не е пипнат!


def test_webhook_mode_mismatch_rejection(db_session: Session) -> None:
    """Събития от live в test режим или обратно се отхвърлят с reason='mode_mismatch'."""
    event_payload = {
        "id": "evt_livemode_mismatch_1",
        "type": "checkout.session.completed",
        "livemode": True,  # Livemode събитие при тест режим
        "data": {"object": {"id": "cs_live_123", "livemode": True}},
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with (
        patch.object(settings, "stripe_mode", "test"),
        patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET),
    ):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "mode_mismatch"


def test_cannot_cancel_or_create_checkout_in_payment_processing(
    db_session: Session,
) -> None:
    """Докато поръчката или опитът се обработват асинхронно, отказът и повторният checkout са забранени (409)."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "Proc Order", stock=5, price_minor=3000)
    order = _create_order(db_session, user.id, product, quantity=1)
    order.status = "payment_processing"

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="processing",
        amount_minor=3000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_proc_block_1",
        checkout_url="https://checkout.stripe.com/pay/cs_proc_block_1",
        idempotency_key="idemp_proc_block_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    # 1. Отказът връща 409
    resp_cancel = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_cancel.status_code == 409
    assert resp_cancel.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"

    # 2. Създаването на нов checkout връща 409
    resp_checkout = client.post(
        f"/orders/{order.id}/checkout-session",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_checkout.status_code == 409
    assert resp_checkout.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"


def test_reconciliation_service_and_rules(db_session: Session) -> None:
    """Тества пълния набор от сценарии за периодичната сверка (scripts/reconcile_orders.py)."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Reconcile Item", stock=100, price_minor=1000)

    now = datetime.datetime.now(datetime.UTC)
    past = now - datetime.timedelta(minutes=40)

    # 1. Поръчка с изтекъл срок и БЕЗ Stripe опит -> expired, stock възстановен
    o1 = _create_order(db_session, user.id, product, quantity=2)  # stock 98
    o1.expires_at = past

    # 2. Поръчка с изтекъл срок и Stripe сесия 'open' -> потвърдено прекратяване в Stripe -> expired, stock възстановен
    o2 = _create_order(db_session, user.id, product, quantity=3)  # stock 95
    o2.expires_at = past
    s2 = StripeCheckoutSession(
        order_id=o2.id,
        status="open",
        amount_minor=3000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_open_2",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_open_2",
        idempotency_key="idemp_rec_2",
    )
    db_session.add(s2)

    # 3. Поръчка с изтекъл срок, но Stripe е недостъпен -> deferred, резервът се пази
    o3 = _create_order(db_session, user.id, product, quantity=1)  # stock 94
    o3.expires_at = past
    s3 = StripeCheckoutSession(
        order_id=o3.id,
        status="open",
        amount_minor=1000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_unavail_3",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_unavail_3",
        idempotency_key="idemp_rec_3",
    )
    db_session.add(s3)

    # 4. Поръчка в payment_processing (отложено плащане) -> deferred, резервът се пази дори при минал срок
    o4 = _create_order(db_session, user.id, product, quantity=4)  # stock 90
    o4.status = "payment_processing"
    o4.expires_at = past
    s4 = StripeCheckoutSession(
        order_id=o4.id,
        status="processing",
        amount_minor=4000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_proc_4",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_proc_4",
        idempotency_key="idemp_rec_4",
    )
    db_session.add(s4)

    # 5. Поръчка с изтекъл срок, но Stripe показва payment_status='paid' -> paid, опитът completed
    o5 = _create_order(db_session, user.id, product, quantity=1)  # stock 89
    o5.expires_at = past
    s5 = StripeCheckoutSession(
        order_id=o5.id,
        status="open",
        amount_minor=1000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_paid_5",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_paid_5",
        idempotency_key="idemp_rec_5",
    )
    db_session.add(s5)
    db_session.commit()

    # Mock StripeClient поведение за всяка сесия
    mock_stripe = MagicMock(spec=StripeClient)

    def mock_retrieve(session_id: str) -> Any:
        m = MagicMock()
        if session_id == "cs_rec_open_2":
            m.status = "open"
            m.payment_status = "unpaid"
            return m
        elif session_id == "cs_rec_unavail_3":
            raise RuntimeError("Stripe API connection timeout")
        elif session_id == "cs_rec_proc_4":
            m.status = "complete"
            m.payment_status = "unpaid"
            return m
        elif session_id == "cs_rec_paid_5":
            m.status = "complete"
            m.payment_status = "paid"
            return m
        raise ValueError(f"Unexpected session_id: {session_id}")

    mock_stripe.retrieve_checkout_session.side_effect = mock_retrieve

    order_repo = OrderRepository(db_session)
    cart_repo = CartRepository(db_session)
    product_repo = ProductRepository(db_session)
    user_repo = UserRepository(db_session)

    service = OrderService(
        order_repository=order_repo,
        cart_repository=cart_repo,
        product_repository=product_repo,
        user_repository=user_repo,
        session=db_session,
        stripe_client=mock_stripe,
    )

    stats = service.reconcile_unresolved_orders(now=now)

    assert stats["checked"] >= 5
    assert stats["expired"] >= 2  # o1 и o2
    assert stats["paid"] >= 1  # o5
    assert stats["deferred"] >= 2  # o3 и o4

    db_session.refresh(o1)
    db_session.refresh(o2)
    db_session.refresh(o3)
    db_session.refresh(o4)
    db_session.refresh(o5)

    assert o1.status == "expired"
    assert o2.status == "expired"
    assert o3.status == "pending_payment"
    assert o4.status == "payment_processing"
    assert o5.status == "paid"

    # Повторна сверка не променя нищо допълнително (идемпотентност)
    stats2 = service.reconcile_unresolved_orders(now=now)
    assert stats2["expired"] == 0


def test_config_stripe_mode_validation() -> None:
    """Валидира настройките за STRIPE_MODE (test vs live) в Settings."""
    # 1. Live режим изисква sk_live_...
    with pytest.raises(
        ValidationError,
        match="In live mode, STRIPE_SECRET_KEY must start with 'sk_live_'",
    ):
        Settings(
            stripe_mode="live",
            stripe_secret_key="sk_test_12345",
            stripe_webhook_secret="whsec_12345",
            stripe_success_url="https://example.com/success/{order_id}",
            stripe_cancel_url="https://example.com/cancel/{order_id}",
        )

    # 2. Live режим изисква webhook_secret
    with pytest.raises(
        ValidationError, match="In live mode, STRIPE_WEBHOOK_SECRET must be set"
    ):
        Settings(
            stripe_mode="live",
            stripe_secret_key="sk_live_12345",
            stripe_webhook_secret=None,
            stripe_success_url="https://example.com/success/{order_id}",
            stripe_cancel_url="https://example.com/cancel/{order_id}",
        )

    # 3. Live режим изисква https URL
    with pytest.raises(ValidationError, match="must start with 'https://'"):
        Settings(
            stripe_mode="live",
            stripe_secret_key="sk_live_12345",
            stripe_webhook_secret="whsec_12345",
            stripe_success_url="http://example.com/success/{order_id}",
            stripe_cancel_url="https://example.com/cancel/{order_id}",
        )

    # 4. Test режим не позволява live ключ (защита от грешка)
    with pytest.raises(
        ValidationError, match="Live key .* cannot be used in test mode"
    ):
        Settings(
            stripe_mode="test",
            stripe_secret_key="sk_live_12345",
        )


# ============================================================================
# 5. Тестове за отложени методи (async payments), срокове (expires_at) и сверка
# ============================================================================


def test_webhook_async_payment_succeeded_marks_paid_stock_unchanged(
    db_session: Session,
) -> None:
    """Асинхронно потвърждение (async_payment_succeeded) превежда поръчката в paid, без промяна на stock."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "SEPA Product", stock=10, price_minor=5000)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock става 8
    order.status = "payment_processing"

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="processing",
        amount_minor=10000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_async_succ_123",
        checkout_url="https://checkout.stripe.com/pay/cs_async_succ_123",
        idempotency_key="idemp_async_succ_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_async_succ_1",
        "type": "checkout.session.async_payment_succeeded",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_async_succ_123",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 10000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert order.status == "paid"
    assert session_rec.status == "completed"
    assert product.stock == 8  # Наличността не се намалява повторно!


def test_webhook_async_payment_failed_resets_pending_if_not_expired(
    db_session: Session,
) -> None:
    """Неуспешно отложено плащане преди изтичане на срока връща поръчката в pending_payment (клиентът може да опита пак)."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "SEPA Fail", stock=5, price_minor=4000)
    order = _create_order(db_session, user.id, product, quantity=1)
    order.status = "payment_processing"
    order.expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        minutes=20
    )

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="processing",
        amount_minor=4000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_async_fail_123",
        checkout_url="https://checkout.stripe.com/pay/cs_async_fail_123",
        idempotency_key="idemp_async_fail_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_async_fail_1",
        "type": "checkout.session.async_payment_failed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_async_fail_123",
                "livemode": False,
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert session_rec.status == "failed"
    assert order.status == "pending_payment"
    assert product.stock == 4  # Резервацията се пази, защото срокът не е изтекъл

    # Клиентът може да създаде нов платежен линк
    mock_client = MagicMock(spec=StripeClient)
    mock_session = MagicMock()
    mock_session.id = "cs_retry_new_999"
    mock_session.url = "https://checkout.stripe.com/pay/cs_retry_new_999"
    mock_client.create_checkout_session.return_value = mock_session

    app.dependency_overrides[get_stripe_client] = lambda: mock_client
    try:
        resp2 = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["checkout_url"] == mock_session.url
    finally:
        app.dependency_overrides.pop(get_stripe_client, None)


def test_webhook_async_payment_failed_expires_order_if_reserve_expired(
    db_session: Session,
) -> None:
    """Неуспешно отложено плащане след изтичане на срока маркира поръчката като expired и връща stock."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "SEPA Expired", stock=5, price_minor=4000)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock става 3
    order.status = "payment_processing"
    order.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=5
    )

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="processing",
        amount_minor=8000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_async_exp_456",
        checkout_url="https://checkout.stripe.com/pay/cs_async_exp_456",
        idempotency_key="idemp_async_exp_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_async_exp_1",
        "type": "checkout.session.async_payment_failed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_async_exp_456",
                "livemode": False,
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert session_rec.status == "failed"
    assert order.status == "expired"
    assert product.stock == 5  # Наличността е възстановена точно веднъж!


def test_webhook_session_expired_event(db_session: Session) -> None:
    """Събитие checkout.session.expired маркира опита expired и освобождава стоката, ако срокът на поръчката е минал."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Timeout Prod", stock=7, price_minor=2000)
    order = _create_order(db_session, user.id, product, quantity=1)  # stock 6
    order.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=1
    )

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=2000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_sess_exp_789",
        checkout_url="https://checkout.stripe.com/pay/cs_sess_exp_789",
        idempotency_key="idemp_sess_exp_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_sess_exp_1",
        "type": "checkout.session.expired",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_sess_exp_789",
                "livemode": False,
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "expired"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert session_rec.status == "expired"
    assert order.status == "expired"
    assert product.stock == 7


def test_webhook_out_of_order_async_success_before_completed_unpaid(
    db_session: Session,
) -> None:
    """Ако async_payment_succeeded пристигне преди completed(unpaid), поръчката не се връща обратно в payment_processing."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Out of Order", stock=5, price_minor=3000)
    order = _create_order(db_session, user.id, product, quantity=1)

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=3000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_ooo_1",
        checkout_url="https://checkout.stripe.com/pay/cs_ooo_1",
        idempotency_key="idemp_ooo_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    # 1. Пристига async_payment_succeeded първо
    evt_succ = {
        "id": "evt_ooo_succ",
        "type": "checkout.session.async_payment_succeeded",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_ooo_1",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 3000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    b_succ = json.dumps(evt_succ).encode()
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=b_succ,
            headers={
                "Stripe-Signature": _generate_stripe_signature(
                    b_succ, TEST_WEBHOOK_SECRET
                )
            },
        )
        assert resp.status_code == 200

    db_session.refresh(order)
    assert order.status == "paid"

    # 2. Закъсняло събитие completed с payment_status=unpaid
    evt_compl = {
        "id": "evt_ooo_compl",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_ooo_1",
                "livemode": False,
                "payment_status": "unpaid",
                "amount_total": 3000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    b_compl = json.dumps(evt_compl).encode()
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=b_compl,
            headers={
                "Stripe-Signature": _generate_stripe_signature(
                    b_compl, TEST_WEBHOOK_SECRET
                )
            },
        )
        assert resp.status_code == 200

    db_session.refresh(order)
    assert order.status == "paid"  # Не е върната в payment_processing!


def test_webhook_discrepancy_payment_on_cancelled_or_expired_order(
    db_session: Session,
) -> None:
    """Плащане върху отказана поръчка връща status='discrepancy' и НЕ променя наличностите тихомълком."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Discrepancy Prod", stock=5, price_minor=2500)
    order = _create_order(db_session, user.id, product, quantity=1)  # stock 4
    # Потребителят е отказал поръчката: наличността е възстановена на 5
    order.status = "cancelled"
    product.stock = 5

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="open",
        amount_minor=2500,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_discrep_1",
        checkout_url="https://checkout.stripe.com/pay/cs_discrep_1",
        idempotency_key="idemp_discrep_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    event_payload = {
        "id": "evt_discrep_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_discrep_1",
                "livemode": False,
                "payment_status": "paid",
                "amount_total": 2500,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
                    "attempt_id": str(session_rec.id),
                },
            }
        },
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "discrepancy"
        assert resp.json()["reason"] == "payment_on_cancelled_order"

    db_session.refresh(order)
    db_session.refresh(session_rec)
    db_session.refresh(product)

    assert order.status == "cancelled"
    assert session_rec.status == "completed"
    assert product.stock == 5  # Не е пипнат!


def test_webhook_mode_mismatch_rejection(db_session: Session) -> None:
    """Събития от live в test режим или обратно се отхвърлят с reason='mode_mismatch'."""
    event_payload = {
        "id": "evt_livemode_mismatch_1",
        "type": "checkout.session.completed",
        "livemode": True,  # Livemode събитие при тест режим
        "data": {"object": {"id": "cs_live_123", "livemode": True}},
    }
    payload_bytes = json.dumps(event_payload).encode()
    sig = _generate_stripe_signature(payload_bytes, TEST_WEBHOOK_SECRET)

    with (
        patch.object(settings, "stripe_mode", "test"),
        patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET),
    ):
        resp = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "mode_mismatch"


def test_cannot_cancel_or_create_checkout_in_payment_processing(
    db_session: Session,
) -> None:
    """Докато поръчката или опитът се обработват асинхронно, отказът и повторният checkout са забранени (409)."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "Proc Order", stock=5, price_minor=3000)
    order = _create_order(db_session, user.id, product, quantity=1)
    order.status = "payment_processing"

    session_rec = StripeCheckoutSession(
        order_id=order.id,
        status="processing",
        amount_minor=3000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_proc_block_1",
        checkout_url="https://checkout.stripe.com/pay/cs_proc_block_1",
        idempotency_key="idemp_proc_block_1",
    )
    db_session.add(session_rec)
    db_session.commit()

    # 1. Отказът връща 409
    resp_cancel = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_cancel.status_code == 409
    assert resp_cancel.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"

    # 2. Създаването на нов checkout връща 409
    resp_checkout = client.post(
        f"/orders/{order.id}/checkout-session",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_checkout.status_code == 409
    assert resp_checkout.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"


def test_reconciliation_service_and_rules(db_session: Session) -> None:
    """Тества пълния набор от сценарии за периодичната сверка (scripts/reconcile_orders.py)."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Reconcile Item", stock=100, price_minor=1000)

    now = datetime.datetime.now(datetime.UTC)
    past = now - datetime.timedelta(minutes=40)

    # 1. Поръчка с изтекъл срок и БЕЗ Stripe опит -> expired, stock възстановен
    o1 = _create_order(db_session, user.id, product, quantity=2)  # stock 98
    o1.expires_at = past

    # 2. Поръчка с изтекъл срок и Stripe сесия 'open' -> потвърдено прекратяване в Stripe -> expired, stock възстановен
    o2 = _create_order(db_session, user.id, product, quantity=3)  # stock 95
    o2.expires_at = past
    s2 = StripeCheckoutSession(
        order_id=o2.id,
        status="open",
        amount_minor=3000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_open_2",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_open_2",
        idempotency_key="idemp_rec_2",
    )
    db_session.add(s2)

    # 3. Поръчка с изтекъл срок, но Stripe е недостъпен -> deferred, резервът се пази
    o3 = _create_order(db_session, user.id, product, quantity=1)  # stock 94
    o3.expires_at = past
    s3 = StripeCheckoutSession(
        order_id=o3.id,
        status="open",
        amount_minor=1000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_unavail_3",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_unavail_3",
        idempotency_key="idemp_rec_3",
    )
    db_session.add(s3)

    # 4. Поръчка в payment_processing (отложено плащане) -> deferred, резервът се пази дори при минал срок
    o4 = _create_order(db_session, user.id, product, quantity=4)  # stock 90
    o4.status = "payment_processing"
    o4.expires_at = past
    s4 = StripeCheckoutSession(
        order_id=o4.id,
        status="processing",
        amount_minor=4000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_proc_4",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_proc_4",
        idempotency_key="idemp_rec_4",
    )
    db_session.add(s4)

    # 5. Поръчка с изтекъл срок, но Stripe показва payment_status='paid' -> paid, опитът completed
    o5 = _create_order(db_session, user.id, product, quantity=1)  # stock 89
    o5.expires_at = past
    s5 = StripeCheckoutSession(
        order_id=o5.id,
        status="open",
        amount_minor=1000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_paid_5",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_paid_5",
        idempotency_key="idemp_rec_5",
    )
    db_session.add(s5)
    db_session.commit()

    # Mock StripeClient поведение за всяка сесия
    mock_stripe = MagicMock(spec=StripeClient)

    def mock_retrieve(session_id: str) -> Any:
        m = MagicMock()
        if session_id == "cs_rec_open_2":
            m.status = "open"
            m.payment_status = "unpaid"
            return m
        elif session_id == "cs_rec_unavail_3":
            raise RuntimeError("Stripe API connection timeout")
        elif session_id == "cs_rec_proc_4":
            m.status = "complete"
            m.payment_status = "unpaid"
            return m
        elif session_id == "cs_rec_paid_5":
            m.status = "complete"
            m.payment_status = "paid"
            return m
        raise ValueError(f"Unexpected session_id: {session_id}")

    mock_stripe.retrieve_checkout_session.side_effect = mock_retrieve

    order_repo = OrderRepository(db_session)
    cart_repo = CartRepository(db_session)
    product_repo = ProductRepository(db_session)
    user_repo = UserRepository(db_session)

    service = OrderService(
        order_repository=order_repo,
        cart_repository=cart_repo,
        product_repository=product_repo,
        user_repository=user_repo,
        session=db_session,
        stripe_client=mock_stripe,
    )

    stats = service.reconcile_unresolved_orders(now=now)

    assert stats["checked"] >= 5
    assert stats["expired"] >= 2  # o1 и o2
    assert stats["paid"] >= 1  # o5
    assert stats["deferred"] >= 2  # o3 и o4

    db_session.refresh(o1)
    db_session.refresh(o2)
    db_session.refresh(o3)
    db_session.refresh(o4)
    db_session.refresh(o5)

    assert o1.status == "expired"
    assert o2.status == "expired"
    assert o3.status == "pending_payment"
    assert o4.status == "payment_processing"
    assert o5.status == "paid"

    # Повторна сверка не променя нищо допълнително (идемпотентност)
    stats2 = service.reconcile_unresolved_orders(now=now)
    assert stats2["expired"] == 0


def test_config_stripe_mode_validation() -> None:
    """Валидира настройките за STRIPE_MODE (test vs live) в Settings."""
    # 1. Live режим изисква sk_live_...
    with pytest.raises(ValidationError, match="In live mode, STRIPE_SECRET_KEY must start with 'sk_live_'"):
        Settings(
            stripe_mode="live",
            stripe_secret_key="sk_test_12345",
            stripe_webhook_secret="whsec_12345",
            stripe_success_url="https://example.com/success/{order_id}",
            stripe_cancel_url="https://example.com/cancel/{order_id}",
        )

    # 2. Live режим изисква webhook_secret
    with pytest.raises(ValidationError, match="In live mode, STRIPE_WEBHOOK_SECRET must be set"):
        Settings(
            stripe_mode="live",
            stripe_secret_key="sk_live_12345",
            stripe_webhook_secret=None,
            stripe_success_url="https://example.com/success/{order_id}",
            stripe_cancel_url="https://example.com/cancel/{order_id}",
        )

    # 3. Live режим изисква https URL
    with pytest.raises(ValidationError, match="must start with 'https://'"):
        Settings(
            stripe_mode="live",
            stripe_secret_key="sk_live_12345",
            stripe_webhook_secret="whsec_12345",
            stripe_success_url="http://example.com/success/{order_id}",
            stripe_cancel_url="https://example.com/cancel/{order_id}",
        )

    # 4. Test режим не позволява live ключ (защита от грешка)
    with pytest.raises(ValidationError, match="Live key .* cannot be used in test mode"):
        Settings(
            stripe_mode="test",
            stripe_secret_key="sk_live_12345",
        )
