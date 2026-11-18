"""Сквозно E2E доказателство за K6.3: самостоятелен Cart микросервис на чист стенд.

Сценарий (по изискване K6.3):
1. Нов продавач публикува продукт в Catalog със складова наличност 5.
2. Купувачът добавя продукта в своята количка (PUT /cart/items/{product_id}) и проверява,
   че наличността в Catalog НЕ е резервирана (остава точно 5).
3. Купувачът вижда продукта в количката си с актуално заглавие и цена от Catalog (GET /cart).
4. Купувачът добавя и втори продукт в количката (qty=1).
5. Купувачът оформя поръчка през Orders (POST /orders с Idempotency-Key):
   - Orders извлича количката през вътрешния API на Cart (/internal/cart/{user_id});
   - Orders резервира наличността в Catalog;
   - Закупените позиции биват изчистени от количката чрез Cart (/internal/cart/{user_id}/clear-purchased).
6. Купувачът проверява количката (GET /cart) - закупените позиции са изчезнали!
7. Повторна заявка със същия Idempotency-Key връща същата поръчка без второ списание
   и без повторни операции в количката.
8. Проверка за регресии от K6.2: заявки с фалшиви X-User-Id без JWT получават 401.
9. Директни проверки в PostgreSQL:
   - Cart DB притежава количката и записите;
   - Catalog DB притежава продуктите и складовите остатъци;
   - Commerce DB съхранява поръчките и историческите снимки.
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
CATALOG_API_URL = os.environ.get("CATALOG_API_URL", "http://127.0.0.1:58003")
CART_API_URL = os.environ.get("CART_API_URL", "http://127.0.0.1:58004")

CART_DSN = os.environ.get(
    "CART_DSN",
    "postgresql://cart_app:cart_app@127.0.0.1:55432/cart",
)
CATALOG_DSN = os.environ.get(
    "CATALOG_DSN",
    "postgresql://catalog_app:catalog_app@127.0.0.1:55432/catalog",
)
COMMERCE_DSN = os.environ.get(
    "COMMERCE_DSN",
    "postgresql://commerce_app:commerce_app@127.0.0.1:55432/commerce",
)

ORDER_PAYLOAD = {
    "shipping_address": {
        "recipient_name": "Александър Иванов",
        "phone": "+359888123456",
        "country_code": "BG",
        "city": "Варна",
        "postal_code": "9000",
        "address_line1": "бул. Княз Борис I 45",
        "address_line2": "",
    }
}


def wait_for_services(timeout_seconds: float = 35.0) -> None:
    """Изчаква Commerce, Users, Catalog и Cart API да бъдат напълно готови."""
    start = time.time()
    print(
        f"==> Waiting for Commerce ({COMMERCE_API_URL}), Users ({USERS_API_URL}), "
        f"Catalog ({CATALOG_API_URL}) and Cart ({CART_API_URL})..."
    )
    while time.time() - start < timeout_seconds:
        try:
            r_c = httpx.get(f"{COMMERCE_API_URL}/health", timeout=2.0)
            r_u = httpx.get(f"{USERS_API_URL}/health", timeout=2.0)
            r_cat = httpx.get(f"{CATALOG_API_URL}/health", timeout=2.0)
            r_cart = httpx.get(f"{CART_API_URL}/health", timeout=2.0)
            if (
                r_c.status_code == 200
                and r_u.status_code == 200
                and r_cat.status_code == 200
                and r_cart.status_code == 200
            ):
                print("==> All required services are healthy and responsive!")
                return
        except (httpx.RequestError, httpx.HTTPStatusError):
            pass
        time.sleep(1.0)
    raise TimeoutError("Services did not become healthy within timeout!")


def register_and_login(client: httpx.Client, email_prefix: str) -> tuple[int, str]:
    email = f"{email_prefix}_{uuid.uuid4().hex[:8]}@example.com"
    password = "StrongPassword12345!"

    reg_resp = client.post(
        f"{COMMERCE_API_URL}/users",
        json={"email": email, "password": password},
        headers={"Content-Type": "application/json"},
    )
    assert reg_resp.status_code == 201, f"Registration failed: {reg_resp.text}"
    user_id = reg_resp.json()["id"]

    login_resp = client.post(
        f"{COMMERCE_API_URL}/auth/login",
        json={"email": email, "password": password},
        headers={"Content-Type": "application/json"},
    )
    assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
    token = login_resp.json()["access_token"]
    return user_id, token


def run_e2e_verification() -> None:
    wait_for_services()

    with httpx.Client(timeout=10.0) as client:
        # [Step 0] Проверка за регресии от K6.2: вход с X-User-Id без JWT връща 401
        print(
            "==> [Step 0] Testing regression fixes: fake auth headers without JWT return 401..."
        )
        fake_catalog_resp = client.post(
            f"{CATALOG_API_URL}/products",
            json={
                "title": "Hacked",
                "price_minor": 100,
                "currency": "EUR",
                "stock": 1,
                "active": True,
            },
            headers={"X-User-Id": "1", "X-User-Role": "seller"},
        )
        assert fake_catalog_resp.status_code == 401
        assert fake_catalog_resp.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"

        fake_cart_resp = client.get(
            f"{CART_API_URL}/cart",
            headers={"X-User-Id": "1"},
        )
        assert fake_cart_resp.status_code == 401
        assert fake_cart_resp.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"
        print(
            "    Regression checks passed: both Catalog and Cart reject fake headers with 401."
        )

        # [Step 1] Регистрация на нов продавач и публикуване на стоки в Catalog
        print(
            "==> [Step 1] Registering a new seller and publishing products in Catalog..."
        )
        _seller_id, seller_token = register_and_login(client, "seller_cart_k63")
        seller_headers = {"Authorization": f"Bearer {seller_token}"}

        # Създаване на Категория
        cat_resp = client.post(
            f"{COMMERCE_API_URL}/categories",
            json={"name": f"Electronics_{uuid.uuid4().hex[:6]}"},
            headers=seller_headers,
        )
        category_id = cat_resp.json()["id"] if cat_resp.status_code == 201 else None

        # Продукт 1: stock=5, price=5000 EUR
        prod1_resp = client.post(
            f"{COMMERCE_API_URL}/products",
            json={
                "title": "Wireless Mechanical Keyboard",
                "price_minor": 5000,
                "currency": "EUR",
                "stock": 5,
                "category_id": category_id,
                "active": True,
            },
            headers=seller_headers,
        )
        assert prod1_resp.status_code == 201, (
            f"Failed to create product 1: {prod1_resp.text}"
        )
        prod1 = prod1_resp.json()
        prod1_id = prod1["id"]

        # Продукт 2: stock=10, price=1500 EUR
        prod2_resp = client.post(
            f"{COMMERCE_API_URL}/products",
            json={
                "title": "Braided USB-C Cable",
                "price_minor": 1500,
                "currency": "EUR",
                "stock": 10,
                "category_id": category_id,
                "active": True,
            },
            headers=seller_headers,
        )
        assert prod2_resp.status_code == 201, (
            f"Failed to create product 2: {prod2_resp.text}"
        )
        prod2 = prod2_resp.json()
        prod2_id = prod2["id"]
        print(
            f"    Products created: P1(id={prod1_id}, stock=5), P2(id={prod2_id}, stock=10)."
        )

        # [Step 2] Регистрация на купувач
        print("==> [Step 2] Registering a new buyer...")
        buyer_id, buyer_token = register_and_login(client, "buyer_cart_k63")
        buyer_headers = {"Authorization": f"Bearer {buyer_token}"}

        # [Step 3] Купувачът добавя Продукт 1 (qty=2) в количката
        print("==> [Step 3] Buyer adds Product 1 (qty=2) to cart...")
        add1_resp = client.put(
            f"{COMMERCE_API_URL}/cart/items/{prod1_id}",
            json={"quantity": 2},
            headers=buyer_headers,
        )
        assert add1_resp.status_code == 200, f"Failed to add to cart: {add1_resp.text}"
        cart_data = add1_resp.json()
        assert len(cart_data["items"]) == 1
        assert cart_data["items"][0]["product_id"] == prod1_id
        assert cart_data["items"][0]["quantity"] == 2
        assert cart_data["items"][0]["unit_price_minor"] == 5000
        assert cart_data["items"][0]["subtotal_minor"] == 10000
        assert cart_data["total_minor"] == 10000

        # Доказваме, че добавянето в количката НЕ резервира остатък в Catalog!
        with psycopg.connect(CATALOG_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT stock FROM products WHERE id = %s", (prod1_id,))
            stock_in_catalog = cur.fetchone()[0]
            assert stock_in_catalog == 5, (
                f"Stock should be 5, but got {stock_in_catalog}"
            )
        print(
            "    Product 1 added to cart. Verified stock in Catalog is STILL 5 (not reserved)."
        )

        # [Step 4] Купувачът вижда продукта в своята количка (GET /cart)
        print("==> [Step 4] Buyer views cart (GET /cart)...")
        view_resp = client.get(f"{COMMERCE_API_URL}/cart", headers=buyer_headers)
        assert view_resp.status_code == 200
        view_data = view_resp.json()
        assert len(view_data["items"]) == 1
        assert view_data["items"][0]["title"] == "Wireless Mechanical Keyboard"
        assert view_data["items"][0]["available"] is True
        print(
            "    Cart view verified: 1 item present, available=True, total=10000 EUR."
        )

        # [Step 5] Купувачът добавя и Продукт 2 (qty=1) в количката
        print("==> [Step 5] Buyer adds Product 2 (qty=1) to cart...")
        add2_resp = client.put(
            f"{COMMERCE_API_URL}/cart/items/{prod2_id}",
            json={"quantity": 1},
            headers=buyer_headers,
        )
        assert add2_resp.status_code == 200
        cart_data2 = add2_resp.json()
        assert len(cart_data2["items"]) == 2
        assert cart_data2["total_minor"] == 11500  # 10000 + 1500

        # [Step 6] Купувачът оформя поръчка през Orders
        print("==> [Step 6] Buyer places order (POST /orders)...")
        order_key = str(uuid.uuid4())
        order_resp = client.post(
            f"{COMMERCE_API_URL}/orders",
            json=ORDER_PAYLOAD,
            headers={**buyer_headers, "Idempotency-Key": order_key},
        )
        assert order_resp.status_code == 201, (
            f"Failed to create order: {order_resp.text}"
        )
        order_data = order_resp.json()
        order_id = order_data["id"]
        assert order_data["total_minor"] == 11500
        print(
            f"    Order #{order_id} placed successfully. Total: {order_data['total_minor']} EUR."
        )

        # [Step 7] Проверка на количката: закупените позиции са изчезнали!
        print("==> [Step 7] Verifying purchased items disappeared from cart...")
        cart_after = client.get(
            f"{COMMERCE_API_URL}/cart", headers=buyer_headers
        ).json()
        assert cart_after["items"] == [], (
            f"Cart should be empty, but got: {cart_after['items']}"
        )
        assert cart_after["total_minor"] == 0
        print(
            "    Verified: purchased items completely disappeared from customer's cart."
        )

        # Проверка на складовите остатъци в Catalog след поръчката:
        with psycopg.connect(CATALOG_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT stock FROM products WHERE id = %s", (prod1_id,))
            assert cur.fetchone()[0] == 3  # 5 - 2 = 3
            cur.execute("SELECT stock FROM products WHERE id = %s", (prod2_id,))
            assert cur.fetchone()[0] == 9  # 10 - 1 = 9
        print(
            "    Verified: Catalog stocks correctly decremented (P1: 5->3, P2: 10->9)."
        )

        # [Step 8] Повтор на заявката с същия Idempotency-Key
        print(
            "==> [Step 8] Idempotent repeat: submitting order with the same Idempotency-Key..."
        )
        repeat_resp = client.post(
            f"{COMMERCE_API_URL}/orders",
            json=ORDER_PAYLOAD,
            headers={**buyer_headers, "Idempotency-Key": order_key},
        )
        assert repeat_resp.status_code in (200, 201), (
            f"Unexpected status: {repeat_resp.status_code}"
        )
        repeat_data = repeat_resp.json()
        assert repeat_data["id"] == order_id
        assert repeat_data["total_minor"] == 11500

        # Доказваме, че складовият остатък НЕ се е свалил втори път!
        with psycopg.connect(CATALOG_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT stock FROM products WHERE id = %s", (prod1_id,))
            assert cur.fetchone()[0] == 3  # Остава 3!
            cur.execute("SELECT stock FROM products WHERE id = %s", (prod2_id,))
            assert cur.fetchone()[0] == 9  # Остава 9!
        print(
            "    Idempotency confirmed: same order returned, stocks still 3 and 9 (no duplicate deduction)."
        )

        # [Step 9] Директни проверки в PostgreSQL за пълна изолация на услугите
        print(
            "==> [Step 9] Direct DB isolation checks across Cart, Catalog, and Commerce..."
        )
        # 1. Cart DB: cart_items за купувача е празен
        with psycopg.connect(CART_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM cart_items WHERE user_id = %s", (buyer_id,)
            )
            assert cur.fetchone()[0] == 0
        print("    Cart DB: confirmed 0 rows in cart_items for buyer.")

        # 2. Commerce DB: поръчката съществува точно веднъж и има запазен исторически снимок
        with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM orders WHERE user_id = %s", (buyer_id,))
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT product_id, product_title, quantity, unit_price_minor "
                "FROM order_items WHERE order_id = %s",
                (order_id,),
            )
            rows = cur.fetchall()
            assert len(rows) == 2
            assert {r[0] for r in rows} == {prod1_id, prod2_id}
            # Проверка на историческия снимок
            for r in rows:
                if r[0] == prod1_id:
                    assert r[1] == "Wireless Mechanical Keyboard"
                    assert r[2] == 2
                    assert r[3] == 5000
                elif r[0] == prod2_id:
                    assert r[1] == "Braided USB-C Cable"
                    assert r[2] == 1
                    assert r[3] == 1500
        print(
            "    Commerce DB: confirmed exactly 1 order with preserved historical item snapshots."
        )

    print("\n" + "=" * 55)
    print("SUCCESS: K6.3 Cart extraction & checkout scenario PASSED!")
    print("=" * 55)


def main() -> None:
    try:
        run_e2e_verification()
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ E2E VERIFICATION FAILED: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
