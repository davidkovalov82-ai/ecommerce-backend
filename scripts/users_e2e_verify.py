"""Сквозно E2E доказателство за реална връзка Commerce -> Users в Docker (без подмени).

Сценарий:
1. Регистрира нов продавач през публичния Commerce адрес (POST /users) -> 201
2. Автентикира продавача (POST /auth/login) и създава продукт (POST /products) -> 201
3. Регистрира нов купувач (POST /users) -> 201 и го автентикира (POST /auth/login) -> 200
4. Отрицателен сценарий: артикул с невалиден/несъществуващ продавач води до 409 CART_PRODUCT_UNAVAILABLE;
   проверява се, че поръчка не е създадена, наличността и количката са непроменени.
5. Положителен сценарий: купувачът поръчва артикула на валидния продавач през POST /orders -> 201;
   проверява се, че поръчката е създадена, наличността е намалена, а количката е изчистена.
6. Директни проверки в PostgreSQL:
   - акаунтите съществуват в базата данни на Users (users.users);
   - акаунтите отсъстват в историческата таблица на Commerce (commerce.users);
   - пренесеният потребител от Commerce съществува в Users.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import psycopg

COMMERCE_API_URL = os.environ.get("COMMERCE_API_URL", "http://127.0.0.1:58000")
USERS_API_URL = os.environ.get("USERS_API_URL", "http://127.0.0.1:58002")

COMMERCE_DSN = os.environ.get(
    "COMMERCE_DSN",
    "postgresql://commerce_app:commerce_app@127.0.0.1:55432/commerce",
)
USERS_DSN = os.environ.get(
    "USERS_DSN",
    "postgresql://users_app:users_app@127.0.0.1:55432/users",
)

ORDER_PAYLOAD = {
    "shipping_address": {
        "recipient_name": "Иван Иванов",
        "phone": "+359888123456",
        "country_code": "BG",
        "city": "София",
        "postal_code": "1000",
        "address_line1": "бул. Витоша 1",
        "address_line2": "ет. 2, ап. 5",
    }
}


def wait_for_services(timeout_seconds: float = 30.0) -> None:
    """Изчаква Commerce и Users API да бъдат достъпни."""
    start = time.time()
    print(
        f"==> Waiting for Commerce ({COMMERCE_API_URL}) and Users ({USERS_API_URL}) to be ready..."
    )
    while time.time() - start < timeout_seconds:
        try:
            r_c = httpx.get(f"{COMMERCE_API_URL}/openapi.json", timeout=2.0)
            r_u = httpx.get(f"{USERS_API_URL}/health", timeout=2.0)
            if r_c.status_code == 200 and r_u.status_code == 200:
                print("==> Both services are ready.")
                return
        except (httpx.HTTPError, httpx.StreamError):
            pass
        time.sleep(1.0)
    raise TimeoutError(f"Services not ready within {timeout_seconds} seconds")


def main() -> None:
    wait_for_services()

    # 1. Регистрация и автентикация на реален продавач през Commerce -> Users
    seller_email = f"e2e_seller_{uuid.uuid4().hex[:10]}@example.com"
    seller_password = "SecurePassword123!"

    print(f"==> Step 1: Register and login real seller ({seller_email})")
    reg_seller = httpx.post(
        f"{COMMERCE_API_URL}/users",
        json={"email": seller_email, "password": seller_password},
        timeout=10.0,
    )
    assert reg_seller.status_code == 201, (
        f"Failed seller registration: {reg_seller.text}"
    )
    seller_id = reg_seller.json()["id"]

    login_seller = httpx.post(
        f"{COMMERCE_API_URL}/auth/login",
        json={"email": seller_email, "password": seller_password},
        timeout=10.0,
    )
    assert login_seller.status_code == 200, f"Failed seller login: {login_seller.text}"
    seller_token = login_seller.json()["access_token"]
    seller_headers = {"Authorization": f"Bearer {seller_token}"}
    print(f"    Seller registered (ID={seller_id}) and authenticated.")

    # Регистрация и промотиране на администратор за модерационни проверки
    admin_email = f"e2e_admin_{uuid.uuid4().hex[:10]}@example.com"
    admin_password = "SecurePassword123!"
    reg_admin = httpx.post(
        f"{COMMERCE_API_URL}/users",
        json={"email": admin_email, "password": admin_password},
        timeout=10.0,
    )
    assert reg_admin.status_code == 201
    admin_id = reg_admin.json()["id"]
    with psycopg.connect(USERS_DSN) as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET role = 'admin' WHERE id = %s", (admin_id,))
        conn.commit()

    login_admin = httpx.post(
        f"{COMMERCE_API_URL}/auth/login",
        json={"email": admin_email, "password": admin_password},
        timeout=10.0,
    )
    assert login_admin.status_code == 200
    admin_token = login_admin.json()["access_token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    print(f"    Admin registered (ID={admin_id}) and authenticated.")

    # 2. Продавачът създава артикул с наличност 10
    print("==> Step 2: Seller creates product (stock=10, price=15.00 EUR)")
    prod_res = httpx.post(
        f"{COMMERCE_API_URL}/products",
        headers=seller_headers,
        json={
            "title": "E2E Тестов артикул",
            "price_minor": 1500,
            "currency": "EUR",
            "stock": 10,
        },
        timeout=10.0,
    )
    assert prod_res.status_code == 201, f"Failed product creation: {prod_res.text}"
    valid_product_id = prod_res.json()["id"]
    print(f"    Product created successfully (ID={valid_product_id}).")

    # 3. Регистрация и автентикация на нов купувач
    buyer_email = f"e2e_buyer_{uuid.uuid4().hex[:10]}@example.com"
    buyer_password = "SecurePassword123!"

    print(f"==> Step 3: Register and login buyer ({buyer_email})")
    reg_buyer = httpx.post(
        f"{COMMERCE_API_URL}/users",
        json={"email": buyer_email, "password": buyer_password},
        timeout=10.0,
    )
    assert reg_buyer.status_code == 201, f"Failed buyer registration: {reg_buyer.text}"
    buyer_id = reg_buyer.json()["id"]

    login_buyer = httpx.post(
        f"{COMMERCE_API_URL}/auth/login",
        json={"email": buyer_email, "password": buyer_password},
        timeout=10.0,
    )
    assert login_buyer.status_code == 200, f"Failed buyer login: {login_buyer.text}"
    buyer_token = login_buyer.json()["access_token"]
    buyer_headers = {"Authorization": f"Bearer {buyer_token}"}
    print(f"    Buyer registered (ID={buyer_id}) and authenticated.")

    # 4. Отрицателен сценарий A: Артикул с несъществуващ продавач в Users (seller_id=999999)
    print(
        "==> Step 4A: Negative scenario - order with non-existent seller (seller_id=999999)"
    )
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO products (title, price_minor, currency, stock, active, seller_id)
            VALUES ('Несъществуващ продавач', 2000, 'EUR', 5, TRUE, 999999)
            RETURNING id;
            """
        )
        invalid_prod_id = cur.fetchone()[0]
        conn.commit()

    put_cart_invalid = httpx.put(
        f"{COMMERCE_API_URL}/cart/items/{invalid_prod_id}",
        headers=buyer_headers,
        json={"quantity": 2},
        timeout=10.0,
    )
    assert put_cart_invalid.status_code == 200, (
        f"Failed adding to cart: {put_cart_invalid.text}"
    )

    fail_order_res = httpx.post(
        f"{COMMERCE_API_URL}/orders",
        headers={**buyer_headers, "Idempotency-Key": str(uuid.uuid4())},
        json=ORDER_PAYLOAD,
        timeout=10.0,
    )
    assert fail_order_res.status_code == 409, (
        f"Expected 409, got: {fail_order_res.status_code} {fail_order_res.text}"
    )
    assert fail_order_res.json()["detail"]["code"] == "CART_PRODUCT_UNAVAILABLE"
    assert fail_order_res.json()["detail"]["product_id"] == invalid_prod_id

    # Проверка на състоянието при отказ: няма поръчка в БД, остатъкът е 5, количката съдържа 1 артикул
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM orders WHERE user_id = %s", (buyer_id,))
        assert cur.fetchone()[0] == 0, "Order was unexpectedly created in database!"
        cur.execute("SELECT stock FROM products WHERE id = %s", (invalid_prod_id,))
        assert cur.fetchone()[0] == 5, "Product stock was modified on failed order!"
    cart_check = httpx.get(
        f"{COMMERCE_API_URL}/cart", headers=buyer_headers, timeout=10.0
    ).json()
    assert len(cart_check["items"]) == 1, "Cart was unexpectedly cleared!"
    del_res = httpx.delete(
        f"{COMMERCE_API_URL}/cart/items/{invalid_prod_id}",
        headers=buyer_headers,
        timeout=10.0,
    )
    assert del_res.status_code in (200, 204)
    print(
        "    Negative scenario A confirmed: 409 CART_PRODUCT_UNAVAILABLE, state preserved."
    )

    # 4B. Отрицателен сценарий B: Блокиран продавач през реалния API на Users
    print(
        "==> Step 4B: Negative scenario - order rejection when seller is blocked via API"
    )
    block_res = httpx.post(
        f"{COMMERCE_API_URL}/users/{seller_id}/block",
        headers=admin_headers,
        json={"reason": "Suspicious activity"},
        timeout=10.0,
    )
    assert block_res.status_code == 200, f"Failed blocking seller: {block_res.text}"
    assert block_res.json()["status"] == "blocked"

    # Купувачът добавя артикула на блокирания продавач
    put_blocked = httpx.put(
        f"{COMMERCE_API_URL}/cart/items/{valid_product_id}",
        headers=buyer_headers,
        json={"quantity": 2},
        timeout=10.0,
    )
    assert put_blocked.status_code == 200

    fail_blocked_res = httpx.post(
        f"{COMMERCE_API_URL}/orders",
        headers={**buyer_headers, "Idempotency-Key": str(uuid.uuid4())},
        json=ORDER_PAYLOAD,
        timeout=10.0,
    )
    assert fail_blocked_res.status_code == 409
    assert fail_blocked_res.json()["detail"]["code"] == "CART_PRODUCT_UNAVAILABLE"
    assert fail_blocked_res.json()["detail"]["product_id"] == valid_product_id

    # Проверка, че поръчка не е създадена, наличността си е 10, количката не е изчистена
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM orders WHERE user_id = %s", (buyer_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT stock FROM products WHERE id = %s", (valid_product_id,))
        assert cur.fetchone()[0] == 10
    cart_check_b = httpx.get(
        f"{COMMERCE_API_URL}/cart", headers=buyer_headers, timeout=10.0
    ).json()
    assert len(cart_check_b["items"]) == 1
    print(
        "    Negative scenario B confirmed: 409 CART_PRODUCT_UNAVAILABLE after seller block."
    )

    # Отблокиране на продавача през API
    unblock_res = httpx.post(
        f"{COMMERCE_API_URL}/users/{seller_id}/unblock",
        headers=admin_headers,
        timeout=10.0,
    )
    assert unblock_res.status_code == 200
    assert unblock_res.json()["status"] == "active"
    print("    Seller successfully unblocked.")

    # 5. Положителен сценарий: Покупка на артикул от активен продавач (утвърден от Users)
    print(
        "==> Step 5: Positive scenario - order with confirmed active seller & Idempotency repeat"
    )
    idemp_key = str(uuid.uuid4())
    order_success_res = httpx.post(
        f"{COMMERCE_API_URL}/orders",
        headers={**buyer_headers, "Idempotency-Key": idemp_key},
        json=ORDER_PAYLOAD,
        timeout=10.0,
    )
    assert order_success_res.status_code == 201, (
        f"Expected 201, got: {order_success_res.status_code} {order_success_res.text}"
    )
    order_data = order_success_res.json()
    created_order_id = order_data["id"]
    print(
        f"    Order successfully placed: Order ID={created_order_id}, Total={order_data['total_minor']} {order_data['currency']}."
    )

    # Проверка на състоянието при успех: поръчката е в БД, остатъкът е намален на 8, количката е празна
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, user_id FROM orders WHERE id = %s", (created_order_id,)
        )
        order_row = cur.fetchone()
        assert order_row is not None and order_row[2] == buyer_id
        cur.execute("SELECT stock FROM products WHERE id = %s", (valid_product_id,))
        assert cur.fetchone()[0] == 8, "Stock was not decremented to 8!"
    cart_after = httpx.get(
        f"{COMMERCE_API_URL}/cart", headers=buyer_headers, timeout=10.0
    ).json()
    assert cart_after["items"] == [], "Cart was not cleared after successful order!"

    # Проверка на повторен опит с идентичен Idempotency-Key: връща същия резултат без повторно списване
    repeat_res = httpx.post(
        f"{COMMERCE_API_URL}/orders",
        headers={**buyer_headers, "Idempotency-Key": idemp_key},
        json=ORDER_PAYLOAD,
        timeout=10.0,
    )
    assert repeat_res.status_code == 201
    assert repeat_res.json()["id"] == created_order_id
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM orders WHERE user_id = %s", (buyer_id,))
        assert cur.fetchone()[0] == 1, "Duplicate order created on idempotent retry!"
        cur.execute("SELECT stock FROM products WHERE id = %s", (valid_product_id,))
        assert cur.fetchone()[0] == 8, (
            "Stock was decremented twice on idempotent retry!"
        )
    print(
        "    Idempotent repeat confirmed: same order returned, stock still 8, no duplicate orders."
    )

    # 6. Сценарий: Количка с двама продавачи (единият е блокиран)
    print("==> Step 6: Multi-seller cart where one seller is blocked")
    seller_2_email = f"e2e_seller2_{uuid.uuid4().hex[:10]}@example.com"
    r_s2 = httpx.post(
        f"{COMMERCE_API_URL}/users",
        json={"email": seller_2_email, "password": "SecurePassword123!"},
        timeout=10.0,
    )
    assert r_s2.status_code == 201
    s2_id = r_s2.json()["id"]
    t_s2 = httpx.post(
        f"{COMMERCE_API_URL}/auth/login",
        json={"email": seller_2_email, "password": "SecurePassword123!"},
        timeout=10.0,
    ).json()["access_token"]
    h_s2 = {"Authorization": f"Bearer {t_s2}"}

    prod_s2_res = httpx.post(
        f"{COMMERCE_API_URL}/products",
        headers=h_s2,
        json={
            "title": "Втори артикул",
            "price_minor": 2500,
            "currency": "EUR",
            "stock": 10,
        },
        timeout=10.0,
    )
    assert prod_s2_res.status_code == 201
    s2_prod_id = prod_s2_res.json()["id"]

    # Блокираме втория продавач
    block_s2 = httpx.post(
        f"{COMMERCE_API_URL}/users/{s2_id}/block",
        headers=admin_headers,
        timeout=10.0,
    )
    assert block_s2.status_code == 200

    # Купувачът добавя артикул 1 (на активния продавач) и артикул 2 (на блокирания продавач)
    httpx.put(
        f"{COMMERCE_API_URL}/cart/items/{valid_product_id}",
        headers=buyer_headers,
        json={"quantity": 2},
        timeout=10.0,
    )
    httpx.put(
        f"{COMMERCE_API_URL}/cart/items/{s2_prod_id}",
        headers=buyer_headers,
        json={"quantity": 3},
        timeout=10.0,
    )

    # Опит за поръчка: отказва целия чек-аут
    multi_fail = httpx.post(
        f"{COMMERCE_API_URL}/orders",
        headers={**buyer_headers, "Idempotency-Key": str(uuid.uuid4())},
        json=ORDER_PAYLOAD,
        timeout=10.0,
    )
    assert multi_fail.status_code == 409
    assert multi_fail.json()["detail"]["code"] == "CART_PRODUCT_UNAVAILABLE"
    assert multi_fail.json()["detail"]["product_id"] == s2_prod_id

    # Проверка: наличността и на двата продукта е непроменена (8 и 10), няма нова поръчка
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM orders WHERE user_id = %s", (buyer_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT stock FROM products WHERE id = %s", (valid_product_id,))
        assert cur.fetchone()[0] == 8
        cur.execute("SELECT stock FROM products WHERE id = %s", (s2_prod_id,))
        assert cur.fetchone()[0] == 10
    cart_multi = httpx.get(
        f"{COMMERCE_API_URL}/cart", headers=buyer_headers, timeout=10.0
    ).json()
    assert len(cart_multi["items"]) == 2
    print(
        "    Multi-seller rejection confirmed: partial order prevented, both stocks intact."
    )

    # Отблокираме Продавач 2 и изпълняваме поръчката успешно
    unblock_s2 = httpx.post(
        f"{COMMERCE_API_URL}/users/{s2_id}/unblock",
        headers=admin_headers,
        timeout=10.0,
    )
    assert unblock_s2.status_code == 200

    multi_success = httpx.post(
        f"{COMMERCE_API_URL}/orders",
        headers={**buyer_headers, "Idempotency-Key": str(uuid.uuid4())},
        json=ORDER_PAYLOAD,
        timeout=10.0,
    )
    assert multi_success.status_code == 201
    multi_data = multi_success.json()
    assert len(multi_data["shipments"]) == 2

    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM orders WHERE user_id = %s", (buyer_id,))
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT stock FROM products WHERE id = %s", (valid_product_id,))
        assert cur.fetchone()[0] == 6  # 8 - 2
        cur.execute("SELECT stock FROM products WHERE id = %s", (s2_prod_id,))
        assert cur.fetchone()[0] == 7  # 10 - 3
    cart_cleared = httpx.get(
        f"{COMMERCE_API_URL}/cart", headers=buyer_headers, timeout=10.0
    ).json()
    assert cart_cleared["items"] == []
    print(
        "    Multi-seller success confirmed: 2 shipments created, stocks decremented (8->6, 10->7), cart empty."
    )

    # 6. Директни проверки в PostgreSQL за изолираност на базите
    print("==> Step 6: Direct DB isolation checks")
    with psycopg.connect(USERS_DSN) as conn, conn.cursor() as cur:
        # Продавачът и купувачът са в Users с активен статус
        for u_id, u_email in [(seller_id, seller_email), (buyer_id, buyer_email)]:
            cur.execute("SELECT id, email, status FROM users WHERE id = %s", (u_id,))
            row = cur.fetchone()
            assert row is not None, f"User {u_email} not found in Users DB!"
            assert row[1] == u_email and row[2] == "active"
        print("    Users DB: Both seller and buyer exist with status=active.")

        # Пренесеният служебен потребител съществува в Users DB
        cur.execute(
            "SELECT id, email FROM users WHERE email = 'legacy-catalog@internal.ecommerce.com'"
        )
        legacy_row = cur.fetchone()
        assert legacy_row is not None, "Migrated legacy user missing in Users DB!"
        print(f"    Users DB: Migrated legacy user found (ID={legacy_row[0]}).")

    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, email FROM users WHERE email IN (%s, %s) OR id IN (%s, %s)",
            (buyer_email, seller_email, buyer_id, seller_id),
        )
        shadow_rows = cur.fetchall()
        assert shadow_rows == [], (
            f"Shadow user rows found in commerce.users: {shadow_rows}!"
        )
        print(
            "    Commerce DB: Zero shadow rows in commerce.users for buyer or seller."
        )

    print("\n✅ SUCCESS: Full Commerce -> Users E2E workflow verified in Docker!")


if __name__ == "__main__":
    main()
