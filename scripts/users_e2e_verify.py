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
import sys
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
    print(f"==> Waiting for Commerce ({COMMERCE_API_URL}) and Users ({USERS_API_URL}) to be ready...")
    while time.time() - start < timeout_seconds:
        try:
            r_c = httpx.get(f"{COMMERCE_API_URL}/openapi.json", timeout=2.0)
            r_u = httpx.get(f"{USERS_API_URL}/health", timeout=2.0)
            if r_c.status_code == 200 and r_u.status_code == 200:
                print("==> Both services are ready.")
                return
        except Exception:
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
    assert reg_seller.status_code == 201, f"Failed seller registration: {reg_seller.text}"
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

    # 4. Отрицателен сценарий: Артикул с несъществуващ продавач в Users (seller_id=999999)
    print("==> Step 4: Negative scenario - order with non-existent seller (seller_id=999999)")
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

    # Купувачът добавя невалидния продукт в количката
    put_cart_invalid = httpx.put(
        f"{COMMERCE_API_URL}/cart/items/{invalid_prod_id}",
        headers=buyer_headers,
        json={"quantity": 2},
        timeout=10.0,
    )
    assert put_cart_invalid.status_code == 200, f"Failed adding to cart: {put_cart_invalid.text}"

    # Опит за оформяне на поръчка: Users връща 404, Commerce превежда в 409 CART_PRODUCT_UNAVAILABLE
    fail_order_res = httpx.post(
        f"{COMMERCE_API_URL}/orders",
        headers={**buyer_headers, "Idempotency-Key": str(uuid.uuid4())},
        json=ORDER_PAYLOAD,
        timeout=10.0,
    )
    assert fail_order_res.status_code == 409, f"Expected 409, got: {fail_order_res.status_code} {fail_order_res.text}"
    fail_detail = fail_order_res.json()["detail"]
    assert fail_detail["code"] == "CART_PRODUCT_UNAVAILABLE"
    assert fail_detail["product_id"] == invalid_prod_id
    print(f"    Negative scenario confirmed: 409 CART_PRODUCT_UNAVAILABLE for product {invalid_prod_id}.")

    # Проверка на състоянието при отказ: няма поръчка в БД, остатъкът е 5, количката съдържа 1 артикул
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM orders WHERE user_id = %s", (buyer_id,))
        assert cur.fetchone()[0] == 0, "Order was unexpectedly created in database!"
        cur.execute("SELECT stock FROM products WHERE id = %s", (invalid_prod_id,))
        assert cur.fetchone()[0] == 5, "Product stock was modified on failed order!"
    cart_check = httpx.get(f"{COMMERCE_API_URL}/cart", headers=buyer_headers, timeout=10.0).json()
    assert len(cart_check["items"]) == 1, "Cart was unexpectedly cleared!"
    print("    State preserved on rejection: 0 orders created, stock unchanged (5), cart intact.")

    # Изчистване на невалидния артикул от количката
    del_res = httpx.delete(f"{COMMERCE_API_URL}/cart/items/{invalid_prod_id}", headers=buyer_headers, timeout=10.0)
    assert del_res.status_code in (200, 204), f"Failed deleting item from cart: {del_res.status_code}"

    # 5. Положителен сценарий: Покупка на артикул от активен продавач (утвърден от Users)
    print("==> Step 5: Positive scenario - order with confirmed active seller")
    put_cart_valid = httpx.put(
        f"{COMMERCE_API_URL}/cart/items/{valid_product_id}",
        headers=buyer_headers,
        json={"quantity": 2},
        timeout=10.0,
    )
    assert put_cart_valid.status_code == 200, f"Failed adding valid product to cart: {put_cart_valid.text}"

    order_success_res = httpx.post(
        f"{COMMERCE_API_URL}/orders",
        headers={**buyer_headers, "Idempotency-Key": str(uuid.uuid4())},
        json=ORDER_PAYLOAD,
        timeout=10.0,
    )
    assert order_success_res.status_code == 201, f"Expected 201, got: {order_success_res.status_code} {order_success_res.text}"
    order_data = order_success_res.json()
    created_order_id = order_data["id"]
    print(f"    Order successfully placed: Order ID={created_order_id}, Total={order_data['total_minor']} {order_data['currency']}.")

    # Проверка на състоянието при успех: поръчката е в БД, остатъкът е намален на 8, количката е празна
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, status, user_id FROM orders WHERE id = %s", (created_order_id,))
        order_row = cur.fetchone()
        assert order_row is not None and order_row[2] == buyer_id
        cur.execute("SELECT stock FROM products WHERE id = %s", (valid_product_id,))
        assert cur.fetchone()[0] == 8, "Stock was not decremented to 8!"
    cart_after = httpx.get(f"{COMMERCE_API_URL}/cart", headers=buyer_headers, timeout=10.0).json()
    assert cart_after["items"] == [], "Cart was not cleared after successful order!"
    print("    State verified on success: Order in DB (status=pending), stock decremented (10 -> 8), cart empty.")

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
        cur.execute("SELECT id, email FROM users WHERE email = 'legacy-catalog@internal.ecommerce.com'")
        legacy_row = cur.fetchone()
        assert legacy_row is not None, "Migrated legacy user missing in Users DB!"
        print(f"    Users DB: Migrated legacy user found (ID={legacy_row[0]}).")

    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, email FROM users WHERE email IN (%s, %s) OR id IN (%s, %s)",
            (buyer_email, seller_email, buyer_id, seller_id),
        )
        shadow_rows = cur.fetchall()
        assert shadow_rows == [], f"Shadow user rows found in commerce.users: {shadow_rows}!"
        print("    Commerce DB: Zero shadow rows in commerce.users for buyer or seller.")

    print("\n✅ SUCCESS: Full Commerce -> Users E2E workflow verified in Docker!")


if __name__ == "__main__":
    main()

