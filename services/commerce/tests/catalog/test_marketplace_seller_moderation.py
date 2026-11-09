import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.modules.accounts.models import User
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderItem
from tests.orders.helpers import ORDER_PAYLOAD, _create_user_and_token

client = TestClient(app)


def _create_admin(db_session: Session) -> tuple[User, str]:
    """Създава тестов администратор."""
    return _create_user_and_token(
        db_session, email="catalog_admin@example.com", role="admin"
    )


def test_seller_blocked_and_unblocked_catalog_and_checkout_flow(
    db_session: Session,
) -> None:
    """1. Пълен маркетплейс сценарий:

    - Анна публикува стока; Борис я вижда и я добавя в количката (available=True).
    - Админ блокира Анна: стоката изчезва от /products и /products/{id} (404).
    - В количката на Борис позицията става available=False.
    - Опитът за checkout от Борис се отхвърля без списан stock и без изчистване на количката.
    - Админ отблокира Анна: стоката отново е видима, в количката е available=True, checkout успява.
    """
    _, admin_token = _create_admin(db_session)

    # 1. Регистрация на Анна (продавач) и Борис (купувач)
    anna_email = "anna_seller_test@example.com"
    anna_pass = "AnnaSellerPass123!"
    client.post("/users", json={"email": anna_email, "password": anna_pass})
    anna_login = client.post(
        "/auth/login", json={"email": anna_email, "password": anna_pass}
    )
    anna_token = anna_login.json()["access_token"]
    anna_user = db_session.query(User).filter_by(email=anna_email).first()
    assert anna_user is not None

    boris_email = "boris_buyer_test@example.com"
    boris_pass = "BorisBuyerPass123!"
    client.post("/users", json={"email": boris_email, "password": boris_pass})
    boris_login = client.post(
        "/auth/login", json={"email": boris_email, "password": boris_pass}
    )
    boris_token = boris_login.json()["access_token"]

    # 2. Анна публикува активен продукт
    prod_res = client.post(
        "/products",
        headers={"Authorization": f"Bearer {anna_token}"},
        json={
            "title": "Anna Ceramic Mug",
            "price_minor": 2500,
            "currency": "EUR",
            "stock": 5,
        },
    )
    assert prod_res.status_code == 201
    prod_id = prod_res.json()["id"]

    # 3. Борис вижда продукта в каталога и в детайли
    catalog_res = client.get("/products")
    assert catalog_res.status_code == 200
    assert any(p["id"] == prod_id for p in catalog_res.json()["items"])

    detail_res = client.get(f"/products/{prod_id}")
    assert detail_res.status_code == 200
    assert detail_res.json()["title"] == "Anna Ceramic Mug"

    # 4. Борис добавя 2 бр. в своята количка
    add_cart_res = client.put(
        f"/cart/items/{prod_id}",
        headers={"Authorization": f"Bearer {boris_token}"},
        json={"quantity": 2},
    )
    assert add_cart_res.status_code == 200

    # Проверка на количката на Борис: позицията е налична (available=True)
    cart_pre = client.get("/cart", headers={"Authorization": f"Bearer {boris_token}"})
    assert cart_pre.status_code == 200
    cart_item_pre = next(
        i for i in cart_pre.json()["items"] if i["product_id"] == prod_id
    )
    assert cart_item_pre["available"] is True

    # 5. Администраторът блокира Анна
    block_res = client.post(
        f"/users/{anna_user.id}/block",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "Investigation of policy compliance"},
    )
    assert block_res.status_code == 200

    # 6. Продуктът на Анна изчезва от публичния каталог и дава 404 на детайли
    catalog_post_block = client.get("/products")
    assert catalog_post_block.status_code == 200
    assert not any(p["id"] == prod_id for p in catalog_post_block.json()["items"])

    detail_post_block = client.get(f"/products/{prod_id}")
    assert detail_post_block.status_code == 404

    # 7. В съществуващата количка на Борис позицията става available=False
    cart_post_block = client.get(
        "/cart", headers={"Authorization": f"Bearer {boris_token}"}
    )
    assert cart_post_block.status_code == 200
    cart_item_blocked = next(
        i for i in cart_post_block.json()["items"] if i["product_id"] == prod_id
    )
    assert cart_item_blocked["available"] is False

    # 8. Опит за checkout от Борис се отхвърля с 409
    idempotency_key = str(uuid.uuid4())
    checkout_blocked_res = client.post(
        "/orders",
        headers={
            "Authorization": f"Bearer {boris_token}",
            "Idempotency-Key": idempotency_key,
        },
        json=ORDER_PAYLOAD,
    )
    assert checkout_blocked_res.status_code == 409

    # Гаранция: наличността в склада НЕ е променена (все още 5)
    db_product = db_session.get(Product, prod_id)
    assert db_product is not None
    assert db_product.stock == 5

    # Гаранция: количката на Борис НЕ е изчистена и пази продукта и количеството
    cart_still_has_item = client.get(
        "/cart", headers={"Authorization": f"Bearer {boris_token}"}
    )
    assert len(cart_still_has_item.json()["items"]) == 1
    assert cart_still_has_item.json()["items"][0]["quantity"] == 2

    # 9. Администраторът отблокира Анна
    unblock_res = client.post(
        f"/users/{anna_user.id}/unblock",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert unblock_res.status_code == 200

    # 10. Продуктът отново се появява в каталога
    catalog_post_unblock = client.get("/products")
    assert any(p["id"] == prod_id for p in catalog_post_unblock.json()["items"])

    detail_post_unblock = client.get(f"/products/{prod_id}")
    assert detail_post_unblock.status_code == 200

    # В количката на Борис отново е available=True
    cart_post_unblock = client.get(
        "/cart", headers={"Authorization": f"Bearer {boris_token}"}
    )
    assert cart_post_unblock.json()["items"][0]["available"] is True

    # 11. Борис завършва поръчката успешно
    new_idempotency_key = str(uuid.uuid4())
    checkout_success_res = client.post(
        "/orders",
        headers={
            "Authorization": f"Bearer {boris_token}",
            "Idempotency-Key": new_idempotency_key,
        },
        json=ORDER_PAYLOAD,
    )
    assert checkout_success_res.status_code == 201

    # Наличността е намалена с 2 (от 5 става 3)
    db_session.expire_all()
    db_product_after = db_session.get(Product, prod_id)
    assert db_product_after is not None
    assert db_product_after.stock == 3

    # Количката на Борис е изчистена след успешната поръчка
    cart_after_success = client.get(
        "/cart", headers={"Authorization": f"Bearer {boris_token}"}
    )
    assert len(cart_after_success.json()["items"]) == 0


def test_seller_deactivated_product_remains_hidden_after_unblock(
    db_session: Session,
) -> None:
    """2. Собственият active флаг на стоката не се перезаписва от блокирането и отблокирането на продавача."""
    _, admin_token = _create_admin(db_session)
    seller, seller_token = _create_user_and_token(
        db_session, email="custom_active_seller@example.com"
    )

    # Създаваме две стоки
    p1_res = client.post(
        "/products",
        headers={"Authorization": f"Bearer {seller_token}"},
        json={
            "title": "Product 1 Active",
            "price_minor": 1000,
            "currency": "EUR",
            "stock": 10,
        },
    )
    p1_id = p1_res.json()["id"]

    p2_res = client.post(
        "/products",
        headers={"Authorization": f"Bearer {seller_token}"},
        json={
            "title": "Product 2 Active",
            "price_minor": 1500,
            "currency": "EUR",
            "stock": 10,
        },
    )
    p2_id = p2_res.json()["id"]

    # Самият продавач деактивира продукт 2
    patch_res = client.patch(
        f"/products/{p2_id}",
        headers={"Authorization": f"Bearer {seller_token}"},
        json={"active": False},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["active"] is False

    # Администраторът блокира продавача
    client.post(
        f"/users/{seller.id}/block",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    # Администраторът отблокира продавача
    client.post(
        f"/users/{seller.id}/unblock",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    # Продукт 1 отново е активен и видим
    p1_get = client.get(f"/products/{p1_id}")
    assert p1_get.status_code == 200

    # Продукт 2 остава деактивиран (active=False) и скрит (404)
    p2_get = client.get(f"/products/{p2_id}")
    assert p2_get.status_code == 404

    # Проверка директно в БД: active на продукт 2 е останал False
    db_p2 = db_session.get(Product, p2_id)
    assert db_p2 is not None
    assert db_p2.active is False


def test_paid_order_preservation_after_seller_closed(
    db_session: Session,
) -> None:
    """3. Закриването на продавача запазва напълно историята на вече платена поръчка."""
    _, admin_token = _create_admin(db_session)
    clara, _ = _create_user_and_token(db_session, email="clara_seller@example.com")
    buyer, buyer_token = _create_user_and_token(
        db_session, email="buyer_order_history@example.com"
    )

    # Клара публикува стока
    prod = Product(
        seller_id=clara.id,
        title="Clara Antique Clock",
        price_minor=15000,
        currency="EUR",
        stock=1,
        active=True,
    )
    db_session.add(prod)
    db_session.commit()
    db_session.refresh(prod)

    # Създаваме платена поръчка на купувача
    order = Order(
        user_id=buyer.id,
        status="paid",
        currency="EUR",
        total_minor=15000,
        idempotency_key=str(uuid.uuid4()),
    )
    db_session.add(order)
    db_session.flush()

    order_item = OrderItem(
        order_id=order.id,
        product_id=prod.id,
        product_title=prod.title,
        quantity=1,
        unit_price_minor=prod.price_minor,
        subtotal_minor=15000,
    )
    db_session.add(order_item)
    db_session.commit()
    order_id = order.id

    # Администраторът закрива акаунта на Клара окончателно
    close_res = client.post(
        f"/users/{clara.id}/close",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "Business closed"},
    )
    assert close_res.status_code == 200

    # Купувачът чете своята поръчка
    order_res = client.get(
        f"/orders/{order_id}",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert order_res.status_code == 200
    order_data = order_res.json()
    assert order_data["status"] == "paid"
    assert order_data["total_minor"] == 15000
    assert len(order_data["items"]) == 1
    assert order_data["items"][0]["product_title"] == "Clara Antique Clock"


def test_put_cart_item_blocked_seller_returns_404_and_preserves_cart(
    db_session: Session,
) -> None:
    """4. Ново добавяне на стока на блокиран продавач дава 404 и не променя количката.

    Позиция, добавена преди блока, остава видима като недостъпна и може да бъде изтрита.
    """
    _, admin_token = _create_admin(db_session)
    seller, seller_token = _create_user_and_token(
        db_session, email="cart_seller_test@example.com"
    )
    _, buyer_token = _create_user_and_token(
        db_session, email="cart_buyer_test@example.com"
    )

    # 1. Продавачът публикува стока
    prod_res = client.post(
        "/products",
        headers={"Authorization": f"Bearer {seller_token}"},
        json={
            "title": "Available Item",
            "price_minor": 3000,
            "currency": "EUR",
            "stock": 10,
        },
    )
    prod_id = prod_res.json()["id"]

    # 2. Купувачът добавя стоката преди блока
    r_add = client.put(
        f"/cart/items/{prod_id}",
        headers={"Authorization": f"Bearer {buyer_token}"},
        json={"quantity": 1},
    )
    assert r_add.status_code == 200

    # 3. Администраторът блокира продавача
    client.post(
        f"/users/{seller.id}/block",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    # 4. Ново добавяне / промяна на количество за стока на блокирания продавач връща 404
    r_blocked_put = client.put(
        f"/cart/items/{prod_id}",
        headers={"Authorization": f"Bearer {buyer_token}"},
        json={"quantity": 2},
    )
    assert r_blocked_put.status_code == 404

    # Количката не е променена (количеството е все още 1)
    cart_res = client.get("/cart", headers={"Authorization": f"Bearer {buyer_token}"})
    assert len(cart_res.json()["items"]) == 1
    assert cart_res.json()["items"][0]["quantity"] == 1
    # Позицията е видима, но недостъпна (available=False)
    assert cart_res.json()["items"][0]["available"] is False

    # 5. Позицията може да бъде изтрита от количката (204)
    r_del = client.delete(
        f"/cart/items/{prod_id}",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert r_del.status_code == 204

    # Количката вече е празна
    cart_after_del = client.get(
        "/cart", headers={"Authorization": f"Bearer {buyer_token}"}
    )
    assert len(cart_after_del.json()["items"]) == 0
