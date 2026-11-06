from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.modules.catalog.models import Product
from app.modules.orders.models import Order
from tests.orders.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
)

client = TestClient(app)


def test_cancel_auth_required() -> None:
    """Критерий: Без автентикация отказът връща 401."""
    resp = client.post("/orders/1/cancel")
    assert resp.status_code == 401


def test_cancel_foreign_or_missing_order_returns_404(
    db_session: Session,
) -> None:
    """Критерий: Чужда или несъществуваща поръчка връща 404 ORDER_NOT_FOUND."""
    user1, token1 = _create_user_and_token(db_session, email="u1@example.com")
    _user2, token2 = _create_user_and_token(db_session, email="u2@example.com")
    product = _create_product(db_session)
    order1 = _create_order(db_session, user1.id, product)

    # Несъществуваща поръчка
    r_non_existent = client.post(
        "/orders/999999/cancel",
        headers={"Authorization": f"Bearer {token1}"},
    )
    assert r_non_existent.status_code == 404
    assert r_non_existent.json()["detail"]["code"] == "ORDER_NOT_FOUND"

    # Чужда поръчка
    r_foreign = client.post(
        f"/orders/{order1.id}/cancel",
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert r_foreign.status_code == 404
    assert r_foreign.json()["detail"]["code"] == "ORDER_NOT_FOUND"


def test_cancel_order_restores_stock_and_double_cancel(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Критерий: Отказ от поръчка възстановява точно запазените бройки. Двоен отказ връща 200 и не дублира stock."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10)
    order = _create_order(db_session, user.id, product, quantity=4)  # stock стана 6
    assert product.stock == 6

    # 1. Първи отказ -> 200 OK
    r1 = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 200
    data1 = r1.json()
    assert data1["id"] == order.id
    assert data1["status"] == "cancelled"

    # Наличността е възстановена с +4 (става отново 10)
    with session_factory() as session:
        db_product = session.get(Product, product.id)
        assert db_product is not None
        assert db_product.stock == 10

        db_order = session.get(Order, order.id)
        assert db_order is not None
        assert db_order.status == "cancelled"

    # 2. Втори отказ (двоен отказ) -> 200 OK (идемпотентно), без повторно връщане на stock
    r2 = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "cancelled"

    # Наличността остава точно 10 (НЕ става 14!)
    with session_factory() as session:
        db_product = session.get(Product, product.id)
        assert db_product is not None
        assert db_product.stock == 10


def test_cancel_order_restores_stock_for_deactivated_product(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Критерий: При отказ stock се възстановява дори за продукт, деактивиран след checkout."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10, active=True)
    order = _create_order(db_session, user.id, product, quantity=3)  # stock=7

    # Деактивираме продукта в каталога
    with session_factory() as session:
        p = session.get(Product, product.id)
        assert p is not None
        p.active = False
        session.commit()

    # Клиентът се отказва от поръчката
    r = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    # Наличността на деактивирания продукт е възстановена (7 -> 10)
    with session_factory() as session:
        refreshed_p = session.get(Product, product.id)
        assert refreshed_p is not None
        assert refreshed_p.active is False
        assert refreshed_p.stock == 10


def test_cancel_paid_order_returns_409(db_session: Session) -> None:
    """Критерий: Опит за отказ на платена поръчка връща 409 ORDER_ALREADY_PAID."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session)
    order = _create_order(db_session, user.id, product, status="paid")

    r = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ORDER_ALREADY_PAID"

