import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from tests.orders.helpers import (
    _add_to_cart,
    _create_product,
    _create_user_and_token,
)

client = TestClient(app)


def test_orders_auth_required() -> None:
    """Критерий: Без валидна автентикация всички поръчкови endpoint-и връщат 401."""
    # POST /orders
    r_post = client.post(
        "/orders",
        json={},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r_post.status_code == 401

    # GET /orders
    r_get_list = client.get("/orders")
    assert r_get_list.status_code == 401

    # GET /orders/{order_id}
    r_get_detail = client.get("/orders/1")
    assert r_get_detail.status_code == 401


def test_get_orders_pagination_and_sorting(db_session: Session) -> None:
    """Критерий: GET /orders връща само собствени поръчки, пагинирани и сортирани по created_at DESC, id DESC."""
    user, token = _create_user_and_token(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    prod = _create_product(db_session, stock=100, price_minor=1000)

    order_ids: list[int] = []
    for _ in range(5):
        _add_to_cart(db_session, user.id, prod.id, quantity=1)
        r = client.post(
            "/orders",
            json={},
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201
        order_ids.append(r.json()["id"])

    # Първа страница: limit 2, offset 0
    r_page1 = client.get("/orders?limit=2&offset=0", headers=headers)
    assert r_page1.status_code == 200
    p1 = r_page1.json()
    assert p1["total"] == 5
    assert p1["limit"] == 2
    assert p1["offset"] == 0
    assert len(p1["items"]) == 2
    # Проверка, че няма вложени items в списъка
    assert "items" not in p1["items"][0]
    # Най-новите са първи
    assert p1["items"][0]["id"] == order_ids[4]
    assert p1["items"][1]["id"] == order_ids[3]

    # Втора страница: limit 2, offset 2
    r_page2 = client.get("/orders?limit=2&offset=2", headers=headers)
    assert r_page2.status_code == 200
    p2 = r_page2.json()
    assert len(p2["items"]) == 2
    assert p2["items"][0]["id"] == order_ids[2]
    assert p2["items"][1]["id"] == order_ids[1]

    # Трета страница: limit 2, offset 4
    r_page3 = client.get("/orders?limit=2&offset=4", headers=headers)
    assert r_page3.status_code == 200
    p3 = r_page3.json()
    assert len(p3["items"]) == 1
    assert p3["items"][0]["id"] == order_ids[0]


def test_get_order_by_id_isolation(db_session: Session) -> None:
    """Критерий: Потребител не може да достъпи чужда поръчка; получава 404 ORDER_NOT_FOUND."""
    user_a, token_a = _create_user_and_token(db_session, email="a@example.com")
    _user_b, token_b = _create_user_and_token(db_session, email="b@example.com")
    prod = _create_product(db_session, stock=10)

    _add_to_cart(db_session, user_a.id, prod.id, quantity=1)
    r_create = client.post(
        "/orders",
        json={},
        headers={
            "Authorization": f"Bearer {token_a}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert r_create.status_code == 201
    order_a_id = r_create.json()["id"]

    # Потребител B опитва да прочете поръчката на A
    r_b = client.get(
        f"/orders/{order_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert r_b.status_code == 404
    assert r_b.json()["detail"]["code"] == "ORDER_NOT_FOUND"

    # Несъществуваща поръчка
    r_none = client.get(
        "/orders/999999",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert r_none.status_code == 404
    assert r_none.json()["detail"]["code"] == "ORDER_NOT_FOUND"
