import concurrent.futures
import threading
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderOutboxEvent, Shipment
from tests.orders.helpers import (
    DEFAULT_SHIPPING_ADDRESS,
    _add_to_cart,
    _create_user_and_token,
)

client = TestClient(app)


def _setup_multi_seller_order(
    db_session: Session,
    status: str = "paid",
    with_address: bool = True,
) -> tuple[dict, dict, dict, Order, Shipment, Shipment]:
    """Помощна функция за създаване на поръчка с двама продавачи (Анна и Борис)."""
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
        seller_id=anna.id,
        title="USB-C кабел",
        price_minor=1500,
        currency="EUR",
        stock=10,
        active=True,
    )
    p3 = Product(
        seller_id=boris.id,
        title="Книга Python",
        price_minor=2000,
        currency="EUR",
        stock=5,
        active=True,
    )
    db_session.add_all([p1, p2, p3])
    db_session.commit()
    db_session.refresh(p1)
    db_session.refresh(p2)
    db_session.refresh(p3)

    _add_to_cart(db_session, clara.id, p1.id, quantity=1)
    _add_to_cart(db_session, clara.id, p2.id, quantity=2)
    _add_to_cart(db_session, clara.id, p3.id, quantity=1)

    headers = {
        "Authorization": f"Bearer {clara_token}",
        "Idempotency-Key": str(uuid.uuid4()),
    }
    payload = {"shipping_address": DEFAULT_SHIPPING_ADDRESS}
    res = client.post("/orders", json=payload, headers=headers)
    assert res.status_code == 201
    order_id = res.json()["id"]

    order = db_session.get(Order, order_id)
    assert order is not None

    if status != "pending_payment":
        order.status = status

    if not with_address:
        order.shipping_recipient_name = None
        order.shipping_phone = None
        order.shipping_country_code = None
        order.shipping_city = None
        order.shipping_postal_code = None
        order.shipping_address_line1 = None
        order.shipping_address_line2 = None

    db_session.commit()
    db_session.refresh(order)

    anna_shipment = next(s for s in order.shipments if s.seller_id == anna.id)
    boris_shipment = next(s for s in order.shipments if s.seller_id == boris.id)

    anna_data = {"user": anna, "token": anna_token}
    boris_data = {"user": boris, "token": boris_token}
    clara_data = {"user": clara, "token": clara_token}

    return (
        anna_data,
        boris_data,
        clara_data,
        order,
        anna_shipment,
        boris_shipment,
    )


def test_seller_shipments_list_and_pagination(db_session: Session) -> None:
    """Продавачът вижда само своите пратки, със стабилна пагинация."""
    anna_data, boris_data, clara_data, _order1, _anna_s1, _ = _setup_multi_seller_order(
        db_session
    )

    # Създаваме втора поръчка с продукт на Анна
    p = Product(
        seller_id=anna_data["user"].id,
        title="Втори продукт на Анна",
        price_minor=3000,
        currency="EUR",
        stock=5,
        active=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)

    _add_to_cart(db_session, clara_data["user"].id, p.id, quantity=1)
    res2 = client.post(
        "/orders",
        json={"shipping_address": DEFAULT_SHIPPING_ADDRESS},
        headers={
            "Authorization": f"Bearer {clara_data['token']}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert res2.status_code == 201

    # Неавтентикиран достъп връща 401
    unauth_res = client.get("/seller/shipments")
    assert unauth_res.status_code == 401

    # Анна има 2 пратки
    anna_res = client.get(
        "/seller/shipments",
        headers={"Authorization": f"Bearer {anna_data['token']}"},
    )
    assert anna_res.status_code == 200
    anna_items = anna_res.json()["items"]
    assert len(anna_items) == 2
    for item in anna_items:
        assert item["seller_id"] == anna_data["user"].id
        assert item["status"] in ("unshipped", "shipped")
        assert "items_count" in item

    # Пагинация
    paged_res = client.get(
        "/seller/shipments?limit=1&offset=0",
        headers={"Authorization": f"Bearer {anna_data['token']}"},
    )
    assert paged_res.status_code == 200
    assert len(paged_res.json()["items"]) == 1

    # Борис има само 1 пратка от първата поръчка
    boris_res = client.get(
        "/seller/shipments",
        headers={"Authorization": f"Bearer {boris_data['token']}"},
    )
    assert boris_res.status_code == 200
    boris_items = boris_res.json()["items"]
    assert len(boris_items) == 1
    assert boris_items[0]["seller_id"] == boris_data["user"].id


def test_seller_shipment_detail_and_data_isolation(db_session: Session) -> None:
    """Детайлите на пратка показват адреса и само нейните позиции, без сумата на цялата поръчка."""
    anna_data, boris_data, clara_data, order, anna_s, _ = _setup_multi_seller_order(
        db_session
    )

    # 1. Анна вижда детайлите на своята пратка
    res = client.get(
        f"/seller/shipments/{anna_s.id}",
        headers={"Authorization": f"Bearer {anna_data['token']}"},
    )
    assert res.status_code == 200
    detail = res.json()

    assert detail["id"] == anna_s.id
    assert detail["order_id"] == order.id
    assert detail["seller_id"] == anna_data["user"].id
    assert detail["status"] == "unshipped"
    assert detail["shipped_at"] is None

    # Адресът за доставка е наличен
    addr = detail["shipping_address"]
    assert addr is not None
    assert addr["recipient_name"] == DEFAULT_SHIPPING_ADDRESS["recipient_name"]
    assert addr["phone"] == DEFAULT_SHIPPING_ADDRESS["phone"]
    assert addr["city"] == DEFAULT_SHIPPING_ADDRESS["city"]
    assert addr["address_line1"] == DEFAULT_SHIPPING_ADDRESS["address_line1"]

    # Позиции: само 2-те позиции на Анна (слушалки и кабел), книгата на Борис липсва
    items = detail["items"]
    assert len(items) == 2
    item_titles = {i["product_title"] for i in items}
    assert item_titles == {"Слушалки Sony", "USB-C кабел"}

    # Данни за цялата поръчка и плащането не се показват
    assert "total_minor" not in detail
    assert "stripe_session_id" not in detail
    assert "payment_intent_id" not in detail

    # 2. Борис опитва да отвори пратката на Анна -> 404 SHIPMENT_NOT_FOUND
    boris_res = client.get(
        f"/seller/shipments/{anna_s.id}",
        headers={"Authorization": f"Bearer {boris_data['token']}"},
    )
    assert boris_res.status_code == 404
    assert boris_res.json()["detail"]["code"] == "SHIPMENT_NOT_FOUND"

    # 3. Купувачът Клара опитва да достъпи като продавач -> 404 SHIPMENT_NOT_FOUND
    clara_res = client.get(
        f"/seller/shipments/{anna_s.id}",
        headers={"Authorization": f"Bearer {clara_data['token']}"},
    )
    assert clara_res.status_code == 404
    assert clara_res.json()["detail"]["code"] == "SHIPMENT_NOT_FOUND"

    # 4. Несъществуваща пратка -> 404
    missing_res = client.get(
        "/seller/shipments/999999",
        headers={"Authorization": f"Bearer {anna_data['token']}"},
    )
    assert missing_res.status_code == 404
    assert missing_res.json()["detail"]["code"] == "SHIPMENT_NOT_FOUND"


def test_ship_unpaid_order_fails_with_409(db_session: Session) -> None:
    """Пратка не може да бъде маркирана като изпратена, ако поръчката не е платена."""
    anna_data, _, _, _, anna_s, _ = _setup_multi_seller_order(
        db_session, status="pending_payment"
    )

    res = client.post(
        f"/seller/shipments/{anna_s.id}/ship",
        headers={"Authorization": f"Bearer {anna_data['token']}"},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "ORDER_NOT_PAID"

    # Пратката остава unshipped
    db_session.refresh(anna_s)
    assert anna_s.status == "unshipped"
    assert anna_s.shipped_at is None


def test_ship_legacy_order_without_address_fails_with_409(
    db_session: Session,
) -> None:
    """Пратка на поръчка без адрес за доставка (напр. наследена) отказва изпращане с 409."""
    anna_data, _, _, _, anna_s, _ = _setup_multi_seller_order(
        db_session, status="paid", with_address=False
    )

    res = client.post(
        f"/seller/shipments/{anna_s.id}/ship",
        headers={"Authorization": f"Bearer {anna_data['token']}"},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "MISSING_SHIPPING_DATA"

    db_session.refresh(anna_s)
    assert anna_s.status == "unshipped"


def test_ship_wrong_seller_returns_404(db_session: Session) -> None:
    """Опит на чужд продавач да изпрати пратка връща 404 SHIPMENT_NOT_FOUND."""
    _anna_data, boris_data, _, _, anna_s, _ = _setup_multi_seller_order(db_session)

    res = client.post(
        f"/seller/shipments/{anna_s.id}/ship",
        headers={"Authorization": f"Bearer {boris_data['token']}"},
    )
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "SHIPMENT_NOT_FOUND"


def test_ship_successful_transition_and_idempotency(db_session: Session) -> None:
    """Успешно изпращане преминава в shipped, записва outbox събитие, а повторно извикване е идемпотентно."""
    anna_data, _, _, order, anna_s, _ = _setup_multi_seller_order(db_session)

    # 1. Първо успешно изпращане
    res = client.post(
        f"/seller/shipments/{anna_s.id}/ship",
        headers={"Authorization": f"Bearer {anna_data['token']}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "shipped"
    assert data["shipped_at"] is not None
    first_shipped_at = data["shipped_at"]

    # Проверка в базата данни
    db_session.refresh(anna_s)
    assert anna_s.status == "shipped"
    assert anna_s.shipped_at is not None

    # Проверка на Outbox събитието
    events = (
        db_session.execute(
            select(OrderOutboxEvent).where(
                OrderOutboxEvent.shipment_id == anna_s.id,
                OrderOutboxEvent.event_type == "shipment.shipped",
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    event = events[0]
    assert event.status == "pending"
    assert event.order_id == order.id
    assert event.payload["shipment_id"] == anna_s.id
    assert event.payload["seller_id"] == anna_data["user"].id

    # 2. Повторно извикване (идемпотентност)
    res2 = client.post(
        f"/seller/shipments/{anna_s.id}/ship",
        headers={"Authorization": f"Bearer {anna_data['token']}"},
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "shipped"
    assert data2["shipped_at"] == first_shipped_at

    # Не се създава второ Outbox събитие
    events2 = (
        db_session.execute(
            select(OrderOutboxEvent).where(
                OrderOutboxEvent.shipment_id == anna_s.id,
                OrderOutboxEvent.event_type == "shipment.shipped",
            )
        )
        .scalars()
        .all()
    )
    assert len(events2) == 1


def test_concurrent_ship_calls(db_session: Session) -> None:
    """Конкурентни заявки за ship към една и съща пратка преминават коректно с точно 1 Outbox събитие."""
    anna_data, _, _, _order, anna_s, _ = _setup_multi_seller_order(db_session)

    shipment_id = anna_s.id
    token = anna_data["token"]

    def _call_ship() -> int:
        c = TestClient(app)
        r = c.post(
            f"/seller/shipments/{shipment_id}/ship",
            headers={"Authorization": f"Bearer {token}"},
        )
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_call_ship) for _ in range(5)]
        results = [f.result() for f in futures]

    assert all(code == 200 for code in results)

    # Проверка: точно 1 Outbox събитие
    events = (
        db_session.execute(
            select(OrderOutboxEvent).where(
                OrderOutboxEvent.shipment_id == shipment_id,
                OrderOutboxEvent.event_type == "shipment.shipped",
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


def test_concurrent_ship_calls_for_different_shipments_same_order(
    db_session: Session,
) -> None:
    """Конкурентни заявки за ship към две различни пратки на една и съща поръчка:

    - И двете заявки успяват (200).
    - Крайният статус на поръчката е 'shipped'.
    - Записани са точно две Outbox събития — по едно за всяка пратка.
    """
    anna_data, boris_data, _, order, anna_s, boris_s = _setup_multi_seller_order(
        db_session
    )

    barrier = threading.Barrier(2)

    def _ship_shipment(shipment_id: int, token: str) -> int:
        barrier.wait()
        c = TestClient(app)
        res = c.post(
            f"/seller/shipments/{shipment_id}/ship",
            headers={"Authorization": f"Bearer {token}"},
        )
        return res.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_ship_shipment, anna_s.id, anna_data["token"])
        f2 = executor.submit(_ship_shipment, boris_s.id, boris_data["token"])
        r1 = f1.result()
        r2 = f2.result()

    assert r1 == 200
    assert r2 == 200

    # Проверка на състоянието на поръчката: трябва да е напълно 'shipped'
    db_session.expire_all()
    db_order = db_session.get(Order, order.id)
    assert db_order is not None
    assert db_order.fulfillment_status == "shipped"
    assert db_order.status == "paid"

    # Проверка на пратките
    db_anna_s = db_session.get(Shipment, anna_s.id)
    db_boris_s = db_session.get(Shipment, boris_s.id)
    assert db_anna_s is not None and db_anna_s.status == "shipped"
    assert db_boris_s is not None and db_boris_s.status == "shipped"

    # Проверка на Outbox събитията: точно 2 събития за тази поръчка
    events = (
        db_session.execute(
            select(OrderOutboxEvent).where(
                OrderOutboxEvent.order_id == order.id,
                OrderOutboxEvent.event_type == "shipment.shipped",
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 2
    assert {ev.shipment_id for ev in events} == {anna_s.id, boris_s.id}
    assert {ev.payload["seller_id"] for ev in events} == {
        anna_data["user"].id,
        boris_data["user"].id,
    }
