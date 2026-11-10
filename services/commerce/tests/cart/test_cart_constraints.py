import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.accounts.models import User
from app.modules.cart.models import CartItem
from app.modules.catalog.models import Product


def _create_user(
    db_session: Session, email: str = "cart_constraints@example.com"
) -> User:
    user = User(
        email=email,
        password_hash="argon2_test_hash",
        role="customer",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _create_product(
    db_session: Session,
    title: str = "Constraint Product",
    price_minor: int = 1000,
    stock: int = 10,
) -> Product:
    product = Product(
        title=title,
        price_minor=price_minor,
        currency="EUR",
        stock=stock,
        active=True,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def test_cart_database_constraints(db_session: Session) -> None:
    """Критерий: FK CASCADE, FK RESTRICT, съставен PK и CHECK constraint на ниво база."""
    user = _create_user(db_session)
    product = _create_product(db_session, stock=10)

    # 1. Валиден запис
    item = CartItem(user_id=user.id, product_id=product.id, quantity=5)
    db_session.add(item)
    db_session.commit()

    # 2. CHECK constraint: quantity < 1 (директно през SQL)
    product_2 = _create_product(db_session, title="P2", stock=10)
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                f"INSERT INTO cart_items (user_id, product_id, quantity) "
                f"VALUES ({user.id}, {product_2.id}, 0)"
            )
        )
        db_session.commit()
    db_session.rollback()

    # 3. CHECK constraint: quantity > 99
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                f"INSERT INTO cart_items (user_id, product_id, quantity) "
                f"VALUES ({user.id}, {product_2.id}, 100)"
            )
        )
        db_session.commit()
    db_session.rollback()

    # 4. Съставен първичен ключ (дублиран запис за същия потребител и продукт)
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                f"INSERT INTO cart_items (user_id, product_id, quantity) "
                f"VALUES ({user.id}, {product.id}, 2)"
            )
        )
        db_session.commit()
    db_session.rollback()

    # 5. FK RESTRICT: опит за изтриване на продукт, който е в количка, бива отхвърлен
    with pytest.raises(IntegrityError):
        db_session.execute(text(f"DELETE FROM products WHERE id = {product.id}"))
        db_session.commit()
    db_session.rollback()

    # 6. След K6.1 няма блокиращ FK към users: cart_items се пазят по логически user_id
    db_session.execute(text(f"DELETE FROM users WHERE id = {user.id}"))
    db_session.commit()
    remaining_items = (
        db_session.query(CartItem).filter(CartItem.user_id == user.id).all()
    )
    assert len(remaining_items) == 1
