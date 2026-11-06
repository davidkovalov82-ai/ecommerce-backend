import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.dependencies import get_db_session
from app.main import app
from app.modules.accounts.models import User
from app.modules.accounts.tokens import create_access_token
from app.modules.cart.models import CartItem
from app.modules.catalog.models import Product

client = TestClient(app)


def _create_user_and_token(
    db_session: Session,
    email: str = "customer_concurrent@example.com",
    role: str = "customer",
) -> tuple[User, str]:
    """Помощна функция за създаване на тестов потребител и валиден Bearer токен."""
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
    title: str = "Concurrent Product",
    price_minor: int = 1000,
    stock: int = 20,
    active: bool = True,
) -> Product:
    """Помощна функция за създаване на тестов продукт."""
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


def test_put_cart_item_concurrency(
    db_session: Session, session_factory: sessionmaker[Session]
) -> None:
    """Критерий: Конкурентни PUT заявки за един и същ потребител и продукт не водят до колизии."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=20)
    headers = {"Authorization": f"Bearer {token}"}

    def _timed_test_db() -> Generator[Session]:
        with session_factory() as session:
            session.execute(text("SET lock_timeout = '3000ms'"))
            session.execute(text("SET statement_timeout = '5000ms'"))
            yield session

    original_override = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = _timed_test_db

    num_workers = 8
    barrier = threading.Barrier(num_workers)

    def _send_put(qty: int) -> int:
        barrier.wait(timeout=5.0)
        r = client.put(
            f"/cart/items/{product.id}",
            json={"quantity": qty},
            headers=headers,
        )
        return r.status_code

    try:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(_send_put, i) for i in range(1, num_workers + 1)]
            status_codes = [f.result(timeout=10.0) for f in futures]
    finally:
        if original_override:
            app.dependency_overrides[get_db_session] = original_override
        else:
            app.dependency_overrides.pop(get_db_session, None)

    assert all(code == 200 for code in status_codes)

    # Проверка в базата: съществува точно един ред за (user_id, product_id)
    db_session.expire_all()
    items = (
        db_session.query(CartItem)
        .filter(CartItem.user_id == user.id, CartItem.product_id == product.id)
        .all()
    )
    assert len(items) == 1
    assert 1 <= items[0].quantity <= num_workers
