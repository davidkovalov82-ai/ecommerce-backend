import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.modules.catalog.models import Product
from app.modules.orders.models import Order
from tests.orders.helpers import (
    DEFAULT_SHIPPING_ADDRESS,
    _add_to_cart,
    _create_user_and_token,
)

client = TestClient(app)


def test_multi_seller_fulfillment_status_progression(db_session: Session) -> None:
    """Критерий: В поръчка с двама продавачи fulfillment_status преминава през:
    unshipped -> partially_shipped -> shipped, докато статусът на плащане остава paid.
    """
    anna, anna_token = _create_user_and_token(
        db_session, email=f"anna_{uuid.uuid4().hex[:6]}@example.com"
    )
    boris, boris_token = _create_user_and_token(
        db_session, email=f"boris_{uuid.uuid4().hex[:6]}@example.com"
    )
    clara, clara_token = _create_user_and_token(
        db_session, email=f"clara_{uuid.uuid4().hex[:6]}@example.com"
    )

    p1 = Product(
        seller_id=anna.id,
        title="Слушалки Sony",
        price_minor=5000,
        currency="EUR",
        stock=10,
        active=True,
    )
    p2 = Product(
        seller_id=boris.id,
        title="Книга Python",
        price_minor=2000,
        currency="EUR",
        stock=5,
        active=True,
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    _add_to_cart(db_session, clara.id, p1.id, quantity=1)
    _add_to_cart(db_session, clara.id, p2.id, quantity=1)

    # 1. Клара създава поръчката
    res = client.post(
        "/orders",
        json={"shipping_address": DEFAULT_SHIPPING_ADDRESS},
        headers={
            "Authorization": f"Bearer {clara_token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert res.status_code == 201
    order_id = res.json()["id"]

    order = db_session.get(Order, order_id)
    assert order is not None
    # Симулираме плащане през Stripe
    order.status = "paid"
    db_session.commit()
    db_session.refresh(order)

    # Първоначално състояние: unshipped
    assert order.fulfillment_status == "unshipped"
    assert order.status == "paid"

    anna_s = next(s for s in order.shipments if s.seller_id == anna.id)
    boris_s = next(s for s in order.shipments if s.seller_id == boris.id)

    # 2. Анна изпраща своята пратка
    res_ship1 = client.post(
        f"/seller/shipments/{anna_s.id}/ship",
        headers={"Authorization": f"Bearer {anna_token}"},
    )
    assert res_ship1.status_code == 200

    # Проверка през купувача: partially_shipped, статусът остава paid
    buyer_res1 = client.get(
        f"/orders/{order_id}",
        headers={"Authorization": f"Bearer {clara_token}"},
    )
    assert buyer_res1.status_code == 200
    b_data1 = buyer_res1.json()
    assert b_data1["status"] == "paid"
    assert b_data1["fulfillment_status"] == "partially_shipped"

    # Пратките в отговора на купувача: пратката на Анна е shipped, на Борис - unshipped
    shipments_map1 = {s["id"]: s["status"] for s in b_data1["shipments"]}
    assert shipments_map1[anna_s.id] == "shipped"
    assert shipments_map1[boris_s.id] == "unshipped"

    # 3. Борис изпраща своята пратка
    res_ship2 = client.post(
        f"/seller/shipments/{boris_s.id}/ship",
        headers={"Authorization": f"Bearer {boris_token}"},
    )
    assert res_ship2.status_code == 200

    # Проверка през купувача: вече е напълно shipped, статусът остава paid
    buyer_res2 = client.get(
        f"/orders/{order_id}",
        headers={"Authorization": f"Bearer {clara_token}"},
    )
    assert buyer_res2.status_code == 200
    b_data2 = buyer_res2.json()
    assert b_data2["status"] == "paid"
    assert b_data2["fulfillment_status"] == "shipped"

    shipments_map2 = {s["id"]: s["status"] for s in b_data2["shipments"]}
    assert shipments_map2[anna_s.id] == "shipped"
    assert shipments_map2[boris_s.id] == "shipped"


def test_single_seller_fulfillment_status_progression(db_session: Session) -> None:
    """В поръчка само от един продавач статусът преминава директно:
    unshipped -> shipped.
    """
    anna, anna_token = _create_user_and_token(
        db_session, email=f"anna_{uuid.uuid4().hex[:6]}@example.com"
    )
    clara, clara_token = _create_user_and_token(
        db_session, email=f"clara_{uuid.uuid4().hex[:6]}@example.com"
    )

    p1 = Product(
        seller_id=anna.id,
        title="Единичен артикул",
        price_minor=3000,
        currency="EUR",
        stock=5,
        active=True,
    )
    db_session.add(p1)
    db_session.commit()

    _add_to_cart(db_session, clara.id, p1.id, quantity=1)

    res = client.post(
        "/orders",
        json={"shipping_address": DEFAULT_SHIPPING_ADDRESS},
        headers={
            "Authorization": f"Bearer {clara_token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert res.status_code == 201
    order_id = res.json()["id"]

    order = db_session.get(Order, order_id)
    assert order is not None
    order.status = "paid"
    db_session.commit()

    assert order.fulfillment_status == "unshipped"
    assert len(order.shipments) == 1
    shipment = order.shipments[0]

    # Анна изпраща
    res_ship = client.post(
        f"/seller/shipments/{shipment.id}/ship",
        headers={"Authorization": f"Bearer {anna_token}"},
    )
    assert res_ship.status_code == 200

    # Проверка на купувача: директно shipped
    buyer_res = client.get(
        f"/orders/{order_id}",
        headers={"Authorization": f"Bearer {clara_token}"},
    )
    assert buyer_res.status_code == 200
    b_data = buyer_res.json()
    assert b_data["status"] == "paid"
    assert b_data["fulfillment_status"] == "shipped"


def test_buyer_order_detail_and_list_contain_shipping_and_shipments(
    db_session: Session,
) -> None:
    """Купувачът вижда адреса за доставка, пратките и статуса fulfillment_status в GET /orders и GET /orders/{id}."""
    anna, _ = _create_user_and_token(
        db_session, email=f"anna_{uuid.uuid4().hex[:6]}@example.com"
    )
    clara, clara_token = _create_user_and_token(
        db_session, email=f"clara_{uuid.uuid4().hex[:6]}@example.com"
    )
    _, stranger_token = _create_user_and_token(
        db_session, email=f"stranger_{uuid.uuid4().hex[:6]}@example.com"
    )

    p = Product(
        seller_id=anna.id,
        title="Тестов артикул",
        price_minor=1200,
        currency="EUR",
        stock=5,
        active=True,
    )
    db_session.add(p)
    db_session.commit()

    _add_to_cart(db_session, clara.id, p.id, quantity=1)

    res = client.post(
        "/orders",
        json={"shipping_address": DEFAULT_SHIPPING_ADDRESS},
        headers={
            "Authorization": f"Bearer {clara_token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert res.status_code == 201
    order_id = res.json()["id"]

    # 1. Списък поръчки на купувача GET /orders
    list_res = client.get(
        "/orders",
        headers={"Authorization": f"Bearer {clara_token}"},
    )
    assert list_res.status_code == 200
    orders = list_res.json()["items"]
    found = next(o for o in orders if o["id"] == order_id)
    assert found["fulfillment_status"] == "unshipped"

    # 2. Детайл на поръчката GET /orders/{id}
    detail_res = client.get(
        f"/orders/{order_id}",
        headers={"Authorization": f"Bearer {clara_token}"},
    )
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["fulfillment_status"] == "unshipped"
    assert detail["shipping_address"]["city"] == DEFAULT_SHIPPING_ADDRESS["city"]
    assert (
        detail["shipping_address"]["recipient_name"]
        == DEFAULT_SHIPPING_ADDRESS["recipient_name"]
    )
    assert len(detail["shipments"]) == 1
    shipment_data = detail["shipments"][0]
    assert shipment_data["seller_id"] == anna.id
    assert shipment_data["status"] == "unshipped"
    assert len(shipment_data["items"]) == 1
    assert shipment_data["items"][0]["product_id"] == p.id

    # 3. Чужд потребител опитва да отвори поръчката -> 404 ORDER_NOT_FOUND
    stranger_res = client.get(
        f"/orders/{order_id}",
        headers={"Authorization": f"Bearer {stranger_token}"},
    )
    assert stranger_res.status_code == 404
    assert stranger_res.json()["detail"]["code"] == "ORDER_NOT_FOUND"
