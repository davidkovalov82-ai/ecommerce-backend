import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.modules.cart.models import CartItem
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderItem
from tests.orders.helpers import (
    ORDER_PAYLOAD,
    _add_to_cart,
    _create_product,
    _create_user_and_token,
)

client = TestClient(app)


def test_late_rollback_on_simulated_error_before_commit(
    db_session: Session,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Критерий: Симулирана грешка след създаване на поръчка, позиции, намаляване на stock и изчистване на количка (след flush, преди commit).

    След rollback през отделна сесия:
    - няма поръчка и позиции;
    - наличността е възстановена;
    - количката е непроменена.
    След премахване на грешката, повторен опит със същия Idempotency-Key успява точно веднъж.
    """
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10, price_minor=1000)
    _add_to_cart(db_session, user.id, product.id, quantity=2)

    idempotency_key = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": idempotency_key,
    }

    original_commit = Session.commit

    def _failing_commit(self: Session) -> None:
        raise RuntimeError("Simulated failure right before commit")

    monkeypatch.setattr(Session, "commit", _failing_commit)

    with pytest.raises(RuntimeError, match="Simulated failure right before commit"):
        client.post("/orders", json=ORDER_PAYLOAD, headers=headers)

    # Проверка през НАПЪЛНО ОТДЕЛНА сесия след rollback
    with session_factory() as verification_session:
        # 1. Няма поръчка и няма позиции от неуспешния опит
        assert (
            verification_session.query(Order).filter(Order.user_id == user.id).count()
            == 0
        )
        assert verification_session.query(OrderItem).count() == 0

        # 2. Наличността е възстановена на първоначалните 10
        refreshed_p = verification_session.get(Product, product.id)
        assert refreshed_p is not None
        assert refreshed_p.stock == 10

        # 3. Количката е непроменена (съдържа продукта с quantity=2)
        cart_items = (
            verification_session.query(CartItem)
            .filter(CartItem.user_id == user.id)
            .all()
        )
        assert len(cart_items) == 1
        assert cart_items[0].product_id == product.id
        assert cart_items[0].quantity == 2

    # Премахваме симулираната грешка
    monkeypatch.setattr(Session, "commit", original_commit)

    # Повтаряме със СЪЩИЯ Idempotency-Key
    r_retry = client.post("/orders", json=ORDER_PAYLOAD, headers=headers)
    assert r_retry.status_code == 201
    order_data = r_retry.json()
    assert order_data["status"] == "pending_payment"
    assert order_data["total_minor"] == 2000
    assert len(order_data["items"]) == 1

    # Проверка през отделна сесия, че поръчката е създадена точно веднъж
    with session_factory() as verification_session:
        assert (
            verification_session.query(Order).filter(Order.user_id == user.id).count()
            == 1
        )
        refreshed_p2 = verification_session.get(Product, product.id)
        assert refreshed_p2 is not None
        assert refreshed_p2.stock == 8
        assert (
            verification_session.query(CartItem)
            .filter(CartItem.user_id == user.id)
            .count()
            == 0
        )

    # Трето повторение със същия ключ -> връща същата поръчка (идемпотентност)
    r_idempotent = client.post("/orders", json=ORDER_PAYLOAD, headers=headers)
    assert r_idempotent.status_code == 201
    assert r_idempotent.json()["id"] == order_data["id"]
