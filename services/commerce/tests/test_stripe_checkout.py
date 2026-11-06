import hashlib
import hmac
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.accounts.models import User
from app.modules.accounts.tokens import create_access_token
from app.modules.catalog.models import Product
from app.modules.orders.dependencies import get_stripe_client
from app.modules.orders.models import (
    Order,
    OrderItem,
    StripeCheckoutSession,
)
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
        assert (
            mock_client.create_checkout_session.call_count == 1
        )  # Няма второ извикване
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

        # В базата трябва да има точно 1 сесиен ред за поръчката
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
    # Празно тяло и липсващ хедър -> 400 за подпис, а НЕ 401 за липсващ JWT
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


def test_webhook_checkout_session_completed_success(db_session: Session) -> None:
    """Успешен webhook маркира поръчката като paid, без повторно намаляване на stock."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=3000, stock=8)
    order = _create_order(db_session, user.id, product, quantity=2)  # total: 6000
    initial_stock = product.stock  # 8 - 2 = 6

    # Добавяме запис за активна сесия в stripe_checkout_sessions
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
        "data": {
            "object": {
                "id": "cs_completed_123",
                "payment_status": "paid",
                "amount_total": 6000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {
                    "order_id": str(order.id),
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

    # Проверка на състоянието в базата данни
    db_session.refresh(order)
    db_session.refresh(product)
    db_session.refresh(session_rec)

    assert order.status == "paid"
    # Stock НЕ е намаляван повторно!
    assert product.stock == initial_stock
    # Сесията е маркирана като completed
    assert session_rec.status == "completed"

    # Събитието е записано в stripe_processed_events
    event_exists = db_session.execute(
        text(
            "SELECT count(*) FROM stripe_processed_events "
            "WHERE stripe_event_id = 'evt_order_paid_123'"
        )
    ).scalar()
    assert event_exists == 1

    # Повторно изпращане на същото събитие -> идемпотентност (already_processed)
    with patch.object(settings, "stripe_webhook_secret", TEST_WEBHOOK_SECRET):
        resp_dup = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig},
        )
        assert resp_dup.status_code == 200
        assert resp_dup.json()["status"] == "already_processed"


def test_webhook_payment_unpaid_leaves_order_pending(db_session: Session) -> None:
    """Неуспешно плащане (напр. отхвърлена карта) оставя поръчката pending_payment."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=2000)
    order = _create_order(db_session, user.id, product, quantity=1)

    event_payload = {
        "id": "evt_order_unpaid_456",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_unpaid_456",
                "payment_status": "unpaid",
                "amount_total": 2000,
                "currency": "eur",
                "client_reference_id": str(order.id),
                "metadata": {"order_id": str(order.id)},
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
        assert resp.json()["status"] == "payment_unpaid"

    db_session.refresh(order)
    assert order.status == "pending_payment"


def test_webhook_mismatched_amount_or_currency(db_session: Session) -> None:
    """Несъответствие в сума или валута не променя поръчката."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "P1", price_minor=3000)
    order = _create_order(db_session, user.id, product, quantity=1)  # 3000 EUR

    # 1. Грешна сума (1000 вместо 3000)
    event_bad_amount = {
        "id": "evt_bad_amt_789",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_bad_amt_789",
                "payment_status": "paid",
                "amount_total": 1000,
                "currency": "eur",
                "metadata": {"order_id": str(order.id)},
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
        "data": {
            "object": {
                "id": "cs_bad_curr_789",
                "payment_status": "paid",
                "amount_total": 3000,
                "currency": "usd",
                "metadata": {"order_id": str(order.id)},
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
    # Несъществуваща поръчка
    evt_ghost = {
        "id": "evt_ghost_999",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_ghost_999",
                "payment_status": "paid",
                "amount_total": 5000,
                "currency": "eur",
                "metadata": {"order_id": "888888"},
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


def test_cancel_order_expires_stripe_session_and_restores_stock(
    db_session: Session,
) -> None:
    """Отказът прекратява активната Stripe сесия и възстановява наличностите (вкл. за деактивиран продукт)."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "Deactivatable", price_minor=2000, stock=5)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock става 3
    assert product.stock == 3

    # Деактивираме продукта след checkout
    product.active = False
    db_session.commit()

    # Създаваме отворена Stripe сесия
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

        # Stripe expire методът е извикан точно с правилното сесийно ID
        mock_client.expire_checkout_session.assert_called_once_with("cs_to_cancel_123")

        # В базата: поръчката е cancelled, наличността е възстановена (3 + 2 = 5)
        db_session.refresh(order)
        db_session.refresh(product)
        db_session.refresh(session_rec)

        assert order.status == "cancelled"
        assert product.stock == 5
        assert product.active is False  # Остава деактивиран
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

    # Симулираме, че по време на извикването на stripe_client.expire_checkout_session,
    # уебхукът маркира поръчката като 'paid'
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
        # Отказът трябва да бъде отказан с 409 ORDER_ALREADY_PAID
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "ORDER_ALREADY_PAID"

        # Проверка: поръчката остава paid, стоката НЕ е върната в stock!
        db_session.refresh(order)
        db_session.refresh(product)
        assert order.status == "paid"
        assert product.stock == 3  # НЕ е 5!
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
        # 1. Извикване на checkout-session с грешка от Stripe
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
            # Дори при изключение, тайният ключ не трябва да присъства в тялото
            # За целта проверяваме, че нашият код не връща ключове
            resp_text = resp.text
            assert secret_value not in resp_text
            assert webhook_secret not in resp_text
        finally:
            app.dependency_overrides.pop(get_stripe_client, None)
