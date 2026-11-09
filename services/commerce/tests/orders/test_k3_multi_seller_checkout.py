import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderItem
from tests.orders.helpers import (
    DEFAULT_SHIPPING_ADDRESS,
    ORDER_PAYLOAD,
    _add_to_cart,
    _create_user_and_token,
)

client = TestClient(app)


def test_multi_seller_order_creation_and_shipment_grouping(
    db_session: Session,
) -> None:
    """Критерий: Три позиции от двама продавача създават една поръчка и две отделни пратки.

    - Анна продава слушалки и кабел;
    - Борис продава книга;
    - Купувачът оформя поръчка: получава 1 поръчка с 3 позиции и 2 пратки;
    - Артикулите на Анна са групирани в нейната пратка;
    - Артикулът на Борис е в неговата пратка;
    - Всяка позиция пази фиксиран seller_id.
    """
    anna, _ = _create_user_and_token(db_session, email="anna_k3_seller@example.com")
    boris, _ = _create_user_and_token(db_session, email="boris_k3_seller@example.com")
    clara, clara_token = _create_user_and_token(
        db_session, email="clara_k3_buyer@example.com"
    )

    # 1. Анна публикува слушалки (p1) и кабел (p2)
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
    # Борис публикува книга (p3)
    p3 = Product(
        seller_id=boris.id,
        title="Python за напреднали",
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

    # 2. Клара добавя и трите продукта в количката
    _add_to_cart(db_session, clara.id, p1.id, quantity=1)
    _add_to_cart(db_session, clara.id, p2.id, quantity=2)
    _add_to_cart(db_session, clara.id, p3.id, quantity=1)

    # 3. Клара оформя поръчката
    idempotency_key = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {clara_token}",
        "Idempotency-Key": idempotency_key,
    }
    payload = {
        "shipping_address": {
            "recipient_name": "Клара Купувачева",
            "phone": "+359888999000",
            "country_code": "BG",
            "city": "Пловдив",
            "postal_code": "4000",
            "address_line1": "ул. Главна 10",
            "address_line2": "ап. 3",
        }
    }
    res = client.post("/orders", json=payload, headers=headers)
    assert res.status_code == 201
    data = res.json()

    # Проверка на общата сума: 5000*1 + 1500*2 + 2000*1 = 10000 цента
    assert data["total_minor"] == 10000
    assert data["currency"] == "EUR"
    assert data["status"] == "pending_payment"
    assert data["fulfillment_status"] == "unshipped"

    # Проверка на адреса за доставка
    addr = data["shipping_address"]
    assert addr is not None
    assert addr["recipient_name"] == "Клара Купувачева"
    assert addr["city"] == "Пловдив"
    assert addr["country_code"] == "BG"

    # Проверка на позициите
    items = data["items"]
    assert len(items) == 3
    items_by_pid = {it["product_id"]: it for it in items}
    assert items_by_pid[p1.id]["seller_id"] == anna.id
    assert items_by_pid[p2.id]["seller_id"] == anna.id
    assert items_by_pid[p3.id]["seller_id"] == boris.id

    # Проверка на пратките
    shipments = data["shipments"]
    assert len(shipments) == 2
    shipments_by_seller = {sh["seller_id"]: sh for sh in shipments}
    assert anna.id in shipments_by_seller
    assert boris.id in shipments_by_seller

    # Пратката на Анна съдържа само нейните стоки (слушалки и кабел)
    anna_sh = shipments_by_seller[anna.id]
    assert anna_sh["status"] == "unshipped"
    anna_sh_pids = {it["product_id"] for it in anna_sh["items"]}
    assert anna_sh_pids == {p1.id, p2.id}

    # Пратката на Борис съдържа само книгата
    boris_sh = shipments_by_seller[boris.id]
    assert boris_sh["status"] == "unshipped"
    boris_sh_pids = {it["product_id"] for it in boris_sh["items"]}
    assert boris_sh_pids == {p3.id}

    # Проверка в базата данни за наличностите
    db_session.expire_all()
    assert db_session.get(Product, p1.id).stock == 9
    assert db_session.get(Product, p2.id).stock == 8
    assert db_session.get(Product, p3.id).stock == 4


def test_shipping_address_validation_and_restrictions(
    db_session: Session,
) -> None:
    """Критерий: Валидация на адрес — забрана на празни стойности, null байтове, непозволени полета."""
    user, token = _create_user_and_token(db_session)
    product = Product(
        seller_id=user.id,
        title="Test item",
        price_minor=1000,
        currency="EUR",
        stock=5,
        active=True,
    )
    db_session.add(product)
    db_session.commit()
    _add_to_cart(db_session, user.id, product.id, quantity=1)

    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
    }

    # 1. Липсващо задължително поле
    invalid_addr1 = dict(DEFAULT_SHIPPING_ADDRESS)
    del invalid_addr1["city"]
    res1 = client.post(
        "/orders", json={"shipping_address": invalid_addr1}, headers=headers
    )
    assert res1.status_code == 422

    # 2. Празно или само с интервали поле
    invalid_addr2 = dict(DEFAULT_SHIPPING_ADDRESS)
    invalid_addr2["recipient_name"] = "   "
    res2 = client.post(
        "/orders", json={"shipping_address": invalid_addr2}, headers=headers
    )
    assert res2.status_code == 422

    # 3. Нулев символ в поле
    invalid_addr3 = dict(DEFAULT_SHIPPING_ADDRESS)
    invalid_addr3["address_line1"] = "ул. Тест\x00 5"
    res3 = client.post(
        "/orders", json={"shipping_address": invalid_addr3}, headers=headers
    )
    assert res3.status_code == 422

    # 4. Невалиден код на държава (не е 2 букви)
    invalid_addr4 = dict(DEFAULT_SHIPPING_ADDRESS)
    invalid_addr4["country_code"] = "BGR"
    res4 = client.post(
        "/orders", json={"shipping_address": invalid_addr4}, headers=headers
    )
    assert res4.status_code == 422

    # 5. Посторонни непозволени полета в адреса
    invalid_addr5 = dict(DEFAULT_SHIPPING_ADDRESS)
    invalid_addr5["extra_custom_field"] = "hack"
    res5 = client.post(
        "/orders", json={"shipping_address": invalid_addr5}, headers=headers
    )
    assert res5.status_code == 422


def test_country_code_normalization_and_strict_ascii_validation(
    db_session: Session,
) -> None:
    """Критерий: country_code приема точно две ASCII-букви с нормализация в горен регистър (bg -> BG).

    Отклонява с 422 преди запис на поръчка: ßa, БГ, цифри (12, B1), невалидна дължина (B, BGR).
    """
    user, token = _create_user_and_token(db_session)
    product = Product(
        seller_id=user.id,
        title="Test item country",
        price_minor=1000,
        currency="EUR",
        stock=10,
        active=True,
    )
    db_session.add(product)
    db_session.commit()
    _add_to_cart(db_session, user.id, product.id, quantity=1)

    # 1. Отхвърляне с 422 за невалидни кодове: ßa, БГ, цифри, дължина
    invalid_codes = ["ßa", "БГ", "12", "B1", "B", "BGR", "DEU", ""]
    for code in invalid_codes:
        addr = dict(DEFAULT_SHIPPING_ADDRESS)
        addr["country_code"] = code
        res = client.post(
            "/orders",
            json={"shipping_address": addr},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        assert res.status_code == 422, (
            f"Expected 422 for country_code='{code}', got {res.status_code}"
        )

    # Уверяваме се, че никаква поръчка не е записана в базата
    assert db_session.query(Order).count() == 0

    # 2. Малки букви 'bg' се нормализират към 'BG'
    valid_addr = dict(DEFAULT_SHIPPING_ADDRESS)
    valid_addr["country_code"] = "bg"
    res_ok = client.post(
        "/orders",
        json={"shipping_address": valid_addr},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert res_ok.status_code == 201
    created_data = res_ok.json()
    assert created_data["shipping_address"]["country_code"] == "BG"

    # Проверка и директно в колоната на Order в базата данни
    db_order = db_session.get(Order, created_data["id"])
    assert db_order is not None
    assert db_order.shipping_country_code == "BG"


def test_idempotency_same_address_and_different_address(
    db_session: Session,
) -> None:
    """Критерий: Повтор на същия адрес връща оригиналната поръчка.

    Различен адрес със същия Idempotency-Key връща 409 без промени.
    """
    user, token = _create_user_and_token(db_session)
    product = Product(
        seller_id=user.id,
        title="Smart Watch",
        price_minor=15000,
        currency="EUR",
        stock=10,
        active=True,
    )
    db_session.add(product)
    db_session.commit()
    _add_to_cart(db_session, user.id, product.id, quantity=1)

    key = str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    # 1. Първо създаване
    res1 = client.post("/orders", json=ORDER_PAYLOAD, headers=headers)
    assert res1.status_code == 201
    order_id = res1.json()["id"]

    # 2. Повтор със същия ключ и същия адрес -> връща оригиналната поръчка
    res2 = client.post("/orders", json=ORDER_PAYLOAD, headers=headers)
    assert res2.status_code == 201
    assert res2.json()["id"] == order_id

    # 3. Опит със същия ключ, но различен адрес -> 409 конфликт
    different_addr = dict(DEFAULT_SHIPPING_ADDRESS)
    different_addr["city"] = "Варна"
    res3 = client.post(
        "/orders",
        json={"shipping_address": different_addr},
        headers=headers,
    )
    assert res3.status_code == 409
    assert res3.json()["detail"]["code"] == "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"

    # Уверяваме се, че поръчката запазва оригиналния си град (София)
    db_session.expire_all()
    saved_order = db_session.get(Order, order_id)
    assert saved_order is not None
    assert saved_order.shipping_city == "София"


def test_legacy_orders_backward_compatibility(db_session: Session) -> None:
    """Критерий: Исторически поръчки без адрес и без пратки се отварят коректно през GET /orders/{id}."""
    user, token = _create_user_and_token(db_session)
    product = Product(
        seller_id=user.id,
        title="Old item",
        price_minor=3000,
        currency="EUR",
        stock=5,
        active=True,
    )
    db_session.add(product)
    db_session.commit()

    # Директно записваме поръчка в базата без адрес (както е изглеждала преди миграцията)
    legacy_order = Order(
        user_id=user.id,
        status="paid",
        fulfillment_status="unshipped",
        currency="EUR",
        total_minor=3000,
        idempotency_key=str(uuid.uuid4()),
        shipping_recipient_name=None,
        shipping_phone=None,
        shipping_country_code=None,
        shipping_city=None,
        shipping_postal_code=None,
        shipping_address_line1=None,
        shipping_address_line2=None,
    )
    db_session.add(legacy_order)
    db_session.flush()

    item = OrderItem(
        order_id=legacy_order.id,
        product_id=product.id,
        seller_id=None,  # Историческа позиция без известен seller_id
        product_title="Old item",
        quantity=1,
        unit_price_minor=3000,
        subtotal_minor=3000,
    )
    db_session.add(item)
    db_session.commit()

    # Прочитаме поръчката през API
    headers = {"Authorization": f"Bearer {token}"}
    res = client.get(f"/orders/{legacy_order.id}", headers=headers)
    assert res.status_code == 200
    data = res.json()

    assert data["id"] == legacy_order.id
    assert data["status"] == "paid"
    assert data["fulfillment_status"] == "unshipped"
    assert data["shipping_address"] is None
    assert data["shipments"] == []
    assert data["items"][0]["seller_id"] is None
