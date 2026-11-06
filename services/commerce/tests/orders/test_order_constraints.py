import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.orders.models import Order, OrderItem
from tests.orders.helpers import _create_product, _create_user_and_token


def test_orders_database_constraints(db_session: Session) -> None:
    """Критерий: UNIQUE, CHECK и FOREIGN KEY ограниченията в orders и order_items работят на ниво PostgreSQL."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session)

    # 1. Валидна поръчка
    valid_order = Order(
        user_id=user.id,
        status="pending_payment",
        currency="EUR",
        total_minor=1000,
        idempotency_key="key-1",
    )
    db_session.add(valid_order)
    db_session.commit()

    # 2. UNIQUE(user_id, idempotency_key) дубликат
    duplicate_key_order = Order(
        user_id=user.id,
        status="pending_payment",
        currency="EUR",
        total_minor=2000,
        idempotency_key="key-1",
    )
    db_session.add(duplicate_key_order)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 3. CHECK status = 'pending_payment'
    invalid_status_order = Order(
        user_id=user.id,
        status="shipped",
        currency="EUR",
        total_minor=1000,
        idempotency_key="key-2",
    )
    db_session.add(invalid_status_order)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 4. CHECK currency = 'EUR'
    invalid_currency_order = Order(
        user_id=user.id,
        status="pending_payment",
        currency="USD",
        total_minor=1000,
        idempotency_key="key-3",
    )
    db_session.add(invalid_currency_order)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 5. RESTRICT на изтриване на потребител с поръчки
    with pytest.raises(IntegrityError):
        db_session.execute(text(f"DELETE FROM users WHERE id = {user.id}"))
        db_session.commit()
    db_session.rollback()

    # 6. RESTRICT на изтриване на продукт с поръчки
    order_item = OrderItem(
        order_id=valid_order.id,
        product_id=product.id,
        product_title=product.title,
        quantity=1,
        unit_price_minor=1000,
        subtotal_minor=1000,
    )
    db_session.add(order_item)
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.execute(text(f"DELETE FROM products WHERE id = {product.id}"))
        db_session.commit()
    db_session.rollback()
