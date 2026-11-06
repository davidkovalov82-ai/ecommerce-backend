from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
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


def test_cancel_order_race_condition_payment_wins(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
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
        with session_factory() as s:
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

