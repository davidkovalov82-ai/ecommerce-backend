import threading
import uuid
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.dependencies import get_db_session
from app.main import app
from app.modules.accounts.repository import UserRepository
from app.modules.cart.models import CartItem
from app.modules.catalog.models import Product
from app.modules.orders.models import Order
from tests.orders.helpers import (
    ORDER_PAYLOAD,
    _add_to_cart,
    _create_product,
    _create_user_and_token,
)

client = TestClient(app)


def test_concurrent_same_key_creates_single_order(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Критерий: Конкурентни заявки с един и същ ключ създават точно една поръчка и не дублират намаляването на склада."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=20, price_minor=1000)
    _add_to_cart(db_session, user.id, product.id, quantity=2)

    key = str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    def _timed_db() -> Generator[Session]:
        with session_factory() as session:
            session.execute(text("SET lock_timeout = '3000ms'"))
            session.execute(text("SET statement_timeout = '5000ms'"))
            yield session

    original_override = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = _timed_db

    num_workers = 6
    barrier = threading.Barrier(num_workers)

    def _send_post() -> tuple[int, int]:
        barrier.wait(timeout=5.0)
        r = client.post("/orders", json=ORDER_PAYLOAD, headers=headers)
        return r.status_code, r.json().get("id", 0)

    try:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(_send_post) for _ in range(num_workers)]
            results = [f.result(timeout=10.0) for f in futures]
    finally:
        if original_override:
            app.dependency_overrides[get_db_session] = original_override
        else:
            app.dependency_overrides.pop(get_db_session, None)

    # Всички заявки трябва да върнат 201 с абсолютно същото order_id
    assert all(status == 201 for status, _ in results)
    order_ids = [order_id for _, order_id in results]
    assert len(set(order_ids)) == 1

    # В базата има точно 1 поръчка и складът е намален точно с 2 (10 - 2 = 18)
    db_session.expire_all()
    assert db_session.query(Order).filter(Order.user_id == user.id).count() == 1
    p = db_session.get(Product, product.id)
    assert p is not None and p.stock == 18


def test_two_users_compete_for_last_stock(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Критерий: Двама потребители купуват последната бройка: една успешна поръчка, един 409 отказ, stock = 0."""
    user_a, token_a = _create_user_and_token(db_session, email="a@example.com")
    user_b, token_b = _create_user_and_token(db_session, email="b@example.com")
    product = _create_product(db_session, stock=1, price_minor=1000)

    _add_to_cart(db_session, user_a.id, product.id, quantity=1)
    _add_to_cart(db_session, user_b.id, product.id, quantity=1)

    def _timed_db() -> Generator[Session]:
        with session_factory() as session:
            session.execute(text("SET lock_timeout = '3000ms'"))
            session.execute(text("SET statement_timeout = '5000ms'"))
            yield session

    original_override = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = _timed_db

    barrier = threading.Barrier(2)

    def _attempt_order(tok: str) -> int:
        barrier.wait(timeout=5.0)
        r = client.post(
            "/orders",
            json=ORDER_PAYLOAD,
            headers={
                "Authorization": f"Bearer {tok}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        return r.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            fa = executor.submit(_attempt_order, token_a)
            fb = executor.submit(_attempt_order, token_b)
            statuses = [fa.result(timeout=10.0), fb.result(timeout=10.0)]
    finally:
        if original_override:
            app.dependency_overrides[get_db_session] = original_override
        else:
            app.dependency_overrides.pop(get_db_session, None)

    # Точно една заявка успява (201), а другата бива отхвърлена (409)
    assert 201 in statuses
    assert 409 in statuses

    db_session.expire_all()
    p = db_session.get(Product, product.id)
    assert p is not None and p.stock == 0
    assert db_session.query(Order).count() == 1


def test_concurrent_checkouts_same_cart_different_keys(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Критерий: Две едновременни оформления на една количка с различни ключове:

    една поръчка, а вторият опит вижда празна количка (409 CART_EMPTY).
    """
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, stock=10, price_minor=1000)
    _add_to_cart(db_session, user.id, product.id, quantity=1)

    def _timed_db() -> Generator[Session]:
        with session_factory() as session:
            session.execute(text("SET lock_timeout = '3000ms'"))
            session.execute(text("SET statement_timeout = '5000ms'"))
            yield session

    original_override = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = _timed_db

    barrier = threading.Barrier(2)

    def _attempt_order() -> int:
        barrier.wait(timeout=5.0)
        r = client.post(
            "/orders",
            json=ORDER_PAYLOAD,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        return r.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(_attempt_order)
            f2 = executor.submit(_attempt_order)
            statuses = [f1.result(timeout=10.0), f2.result(timeout=10.0)]
    finally:
        if original_override:
            app.dependency_overrides[get_db_session] = original_override
        else:
            app.dependency_overrides.pop(get_db_session, None)

    # Едната успява (201), а втората вижда празна количка (409)
    assert 201 in statuses
    assert 409 in statuses

    db_session.expire_all()
    assert db_session.query(Order).filter(Order.user_id == user.id).count() == 1


@pytest.mark.parametrize("scenario", ["put_first", "checkout_first"])
def test_parallel_checkout_and_cart_modification(
    db_session: Session,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """Критерий: Паралелен checkout и промяна на количката се сериализират от общата DB блокировка на потребителя.

    Доказва, че никога не се губят позиции при нито един от двата реда на изпълнение:
    А) PUT се изпълнява преди checkout:
       - p2 присъства в поръчката с quantity=3;
       - p2 липсва от количката;
       - stock на p2 е 7.
    Б) Checkout се изпълнява преди PUT:
       - p2 липсва от поръчката;
       - p2 остава в количката с quantity=3;
       - stock на p2 е 10.
    И в двата случая:
       - p1 присъства в поръчката с quantity=1 и stock на p1 е 9;
       - p2 не липсва и от поръчката, и от количката;
       - няма дублиране на позиции.
    """
    user, token = _create_user_and_token(db_session)
    p1 = _create_product(db_session, stock=10, price_minor=1000)
    p2 = _create_product(db_session, stock=10, price_minor=2000)
    _add_to_cart(db_session, user.id, p1.id, quantity=1)

    def _sync_db(request: Request) -> Generator[Session]:
        with session_factory() as session:
            session.execute(text("SET lock_timeout = '3000ms'"))
            session.execute(text("SET statement_timeout = '5000ms'"))
            if request.method == "POST" and request.url.path.rstrip("/") == "/orders":
                session.info["operation"] = "checkout"
            elif request.method == "PUT" and request.url.path.startswith(
                "/cart/items/"
            ):
                session.info["operation"] = "cart_put"
            yield session

    original_override = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = _sync_db

    first_op = "cart_put" if scenario == "put_first" else "checkout"
    second_op = "checkout" if scenario == "put_first" else "cart_put"

    op1_locked = threading.Event()
    op2_about_to_lock = threading.Event()
    op1_proceed = threading.Event()

    original_lock_user = UserRepository.lock_user

    def _sync_lock_user(self: UserRepository, user_id: int):
        op = self._session.info.get("operation")
        if op == first_op:
            # 1. Първата операция действително придобива DB блокировката:
            res = original_lock_user(self, user_id)
            op1_locked.set()

            # 3. Първата операция получава разрешение да продължи:
            if not op1_proceed.wait(timeout=5.0):
                raise TimeoutError(
                    f"Operation 1 ({first_op}) timed out waiting for permission to proceed"
                )
            return res

        if op == second_op:
            # Изчакваме първата операция действително да придобие DB блокировката:
            if not op1_locked.wait(timeout=5.0):
                raise TimeoutError(
                    f"Operation 2 ({second_op}) timed out waiting for operation 1 ({first_op}) to acquire DB lock"
                )

            # 2. Втората операция достига опита за придобиване на същата блокировка:
            op2_about_to_lock.set()
            # Разрешаваме на първата операция да продължи:
            op1_proceed.set()

            # Втората операция се опитва да придобие същата блокировка (блокира се в PostgreSQL):
            return original_lock_user(self, user_id)

        return original_lock_user(self, user_id)

    monkeypatch.setattr(UserRepository, "lock_user", _sync_lock_user)

    idempotency_key = str(uuid.uuid4())

    def _do_checkout() -> int:
        r = client.post(
            "/orders",
            json=ORDER_PAYLOAD,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": idempotency_key,
            },
        )
        return r.status_code

    def _do_cart_put() -> int:
        r = client.put(
            f"/cart/items/{p2.id}",
            json={"quantity": 3},
            headers={"Authorization": f"Bearer {token}"},
        )
        return r.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            f_checkout = executor.submit(_do_checkout)
            f_cart = executor.submit(_do_cart_put)
            checkout_status = f_checkout.result(timeout=10.0)
            cart_status = f_cart.result(timeout=10.0)
    finally:
        # Освобождаваме чакащи операции във finally при всякакви обстоятелства
        op1_locked.set()
        op2_about_to_lock.set()
        op1_proceed.set()
        monkeypatch.undo()
        if original_override:
            app.dependency_overrides[get_db_session] = original_override
        else:
            app.dependency_overrides.pop(get_db_session, None)

    assert op1_locked.is_set(), f"Operation 1 ({first_op}) failed to acquire lock"
    assert op2_about_to_lock.is_set(), (
        f"Operation 2 ({second_op}) failed to reach lock attempt"
    )
    assert op1_proceed.is_set(), (
        f"Operation 1 ({first_op}) never received permission to proceed"
    )
    assert checkout_status == 201
    assert cart_status == 200

    # Проверка през отделна чиста сесия
    with session_factory() as session:
        order = session.query(Order).filter(Order.user_id == user.id).one()
        order_items_by_pid = {item.product_id: item for item in order.items}
        cart_items_by_pid = {
            item.product_id: item
            for item in session.query(CartItem)
            .filter(CartItem.user_id == user.id)
            .all()
        }
        refreshed_p1 = session.get(Product, p1.id)
        refreshed_p2 = session.get(Product, p2.id)

        assert refreshed_p1 is not None
        assert refreshed_p2 is not None

        # Проверка на p1 (винаги е в поръчката с quantity=1, а наличността му става 9)
        assert p1.id in order_items_by_pid
        assert order_items_by_pid[p1.id].quantity == 1
        assert refreshed_p1.stock == 9
        assert p1.id not in cart_items_by_pid

        p2_in_order = p2.id in order_items_by_pid
        p2_in_cart = p2.id in cart_items_by_pid

        # Доказваме, че p2 не липсва и от поръчката, и от количката!
        assert p2_in_order or p2_in_cart
        # И че не се дублира едновременно в двете
        assert not (p2_in_order and p2_in_cart)

        if scenario == "put_first":
            # А) PUT се изпълнява преди checkout:
            # - p2 присъства в поръчката с quantity=3;
            # - p2 липсва от количката;
            # - stock на p2 е 7.
            assert p2_in_order
            assert not p2_in_cart
            assert order_items_by_pid[p2.id].quantity == 3
            assert refreshed_p2.stock == 7
            assert order.total_minor == 1 * 1000 + 3 * 2000  # 7000
        else:
            # Б) Checkout се изпълнява преди PUT:
            # - p2 липсва от поръчката;
            # - p2 остава в количката с quantity=3;
            # - stock на p2 е 10.
            assert not p2_in_order
            assert p2_in_cart
            assert cart_items_by_pid[p2.id].quantity == 3
            assert refreshed_p2.stock == 10
            assert order.total_minor == 1 * 1000  # 1000
