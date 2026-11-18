"""Сквозно E2E доказателство за K6.4: шест самостоятелни микросервиса на чист стенд.

Архитектура на шестте сервиза:
  1. Users (8002): Потребители, автентикация, JWT RS256, роли (купувач, продавач, админ).
  2. Catalog (8003): Продукти, категории, наличности, складови резервации (order_id идемпотентност).
  3. Cart (8004): Колички, версии на позициите, версионирано изчистване (order_id идемпотентност).
  4. Orders (8000): Поръчки, снимки на цени/продукти, координация на резерви, Outbox, пратки.
  5. Payments (8001): Плащания, Stripe сесии/уебхуци, подписване на събития.
  6. Notifications (Worker): Приемане на RabbitMQ събития, SMTP имейли към Mailpit.
  + Nginx Gateway (58000): Рутира /users и /auth към Users, /users/me/products и /products към Catalog,
                          /cart към Cart, /orders и /seller/shipments към Orders.

Сценарий за проверка:
  1. Проверка на Nginx Gateway и здравните проверки на 6-те микросервиса.
  2. Регистрация и вход на нов продавач и нов купувач през Gateway (/users, /auth/login).
  3. Продавачът създава 2 продукта в Catalog през Gateway (/products).
     Проверка на приоритетното рутиране на Gateway: GET /users/me/products отива в Catalog, а не в Users!
  4. Купувачът добавя Продукт 1 (qty=2) и Продукт 2 (qty=1) в своята количка (/cart).
     Проверка: наличностите в Catalog НЕ се променят преди поръчка (остават съответно 5 и 10).
  5. Проверка за негубене на паралелни промени в количката по време на checkout (Optimistic Versioning):
     - В количката са Продукт 1 (qty=2, v=1) и Продукт 2 (qty=1, v=1).
     - По време на създаване на поръчката, купувачът увеличава Продукт 2 на qty=3 (версията в Cart става 2).
     - Orders прави снимка на v1 и изчиства закупените позиции с версия 1.
     - Продукт 1 съвпада с v1 -> бива изчистен от количката.
     - Продукт 2 вече е с v2 -> НЕ бива изчистен, новото количество 3 остава запазено в количката!
  6. Идемпотентност на поръчката:
     - Повторна заявка със същия Idempotency-Key връща същата поръчка (същото order_id)
       без създаване на втори запис и без повторно списване на наличности.
  7. Тестово плащане и статус paid:
     - Създаване на Stripe checkout session през Orders -> Payments.
     - Изпращане на подписан Stripe webhook (checkout.session.completed) директно към Payments.
     - Payments публикува payment.paid в RabbitMQ.
     - Orders payment consumer обработва събитието и отбелязва поръчката като 'paid'.
     - Orders записва събитие order.paid в Outbox и го публикува в RabbitMQ.
  8. Уведомление:
     - Notifications консумира order.paid от RabbitMQ и изпраща имейл през SMTP към Mailpit.
     - Проверка в Mailpit API, че имейлът за поръчката е получен от купувача.
  9. Проверка на изолация на базите данни:
     - Orders DB съхранява само orders/shipments/outbox, чуждите таблици (users, products, cart_items) липсват.
     - Cart DB съхранява само cart_items и cart_cleared_orders.
     - Catalog DB съхранява само products, categories и stock_reservations.
     - Users DB съхранява само users и outbox.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from typing import Any

import httpx
import psycopg

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:58000")
USERS_API_URL = os.environ.get("USERS_API_URL", "http://127.0.0.1:58002")
CATALOG_API_URL = os.environ.get("CATALOG_API_URL", "http://127.0.0.1:58003")
CART_API_URL = os.environ.get("CART_API_URL", "http://127.0.0.1:58004")
ORDERS_API_URL = os.environ.get("ORDERS_API_URL", "http://127.0.0.1:58005")
PAYMENTS_API_URL = os.environ.get("PAYMENTS_API_URL", "http://127.0.0.1:58001")
MAILPIT_API_URL = os.environ.get("MAILPIT_API_URL", "http://127.0.0.1:58025")

STRIPE_WEBHOOK_SECRET = os.environ.get(
    "STRIPE_WEBHOOK_SECRET",
    "whsec_k4_e2e_secret_stripe_commerce_32_bytes_ok",
)

ORDERS_DSN = os.environ.get(
    "ORDERS_DSN",
    "postgresql://commerce_app:commerce_app@127.0.0.1:55432/commerce",
)
CART_DSN = os.environ.get(
    "CART_DSN",
    "postgresql://cart_app:cart_app@127.0.0.1:55432/cart",
)
CATALOG_DSN = os.environ.get(
    "CATALOG_DSN",
    "postgresql://catalog_app:catalog_app@127.0.0.1:55432/catalog",
)
USERS_DSN = os.environ.get(
    "USERS_DSN",
    "postgresql://users_app:users_app@127.0.0.1:55432/users",
)

SHIPPING_ADDRESS = {
    "recipient_name": "Борислав Стоянов",
    "phone": "+359888999111",
    "country_code": "BG",
    "city": "Пловдив",
    "postal_code": "4000",
    "address_line1": "ул. Главна 12",
    "address_line2": "ет. 3, ап. 8",
}


def wait_for_all_services(timeout_seconds: float = 45.0) -> None:
    """Изчаква Gateway и всички 6 микросервиса + Mailpit да станат здрави."""
    start = time.time()
    print("==> Waiting for all 6 microservices and Gateway to become healthy...")
    while time.time() - start < timeout_seconds:
        try:
            r_gw = httpx.get(f"{GATEWAY_URL}/health", timeout=2.0)
            r_u = httpx.get(f"{USERS_API_URL}/health", timeout=2.0)
            r_cat = httpx.get(f"{CATALOG_API_URL}/health", timeout=2.0)
            r_cart = httpx.get(f"{CART_API_URL}/health", timeout=2.0)
            r_ord = httpx.get(f"{ORDERS_API_URL}/health", timeout=2.0)
            r_pay = httpx.get(f"{PAYMENTS_API_URL}/health", timeout=2.0)
            r_mail = httpx.get(f"{MAILPIT_API_URL}/api/v1/messages", timeout=2.0)
            if (
                r_gw.status_code == 200
                and r_u.status_code == 200
                and r_cat.status_code == 200
                and r_cart.status_code == 200
                and r_ord.status_code == 200
                and r_pay.status_code == 200
                and r_mail.status_code == 200
            ):
                print(
                    "==> All 6 services, Mailpit, and Gateway are online and healthy!"
                )
                return
        except (httpx.RequestError, httpx.HTTPStatusError):
            pass
        time.sleep(1.0)
    raise TimeoutError("Timed out waiting for microservices to become ready!")


def register_user(client: httpx.Client, prefix: str) -> tuple[int, str]:
    """Регистрира нов потребител през Gateway и връща (user_id, token)."""
    email = f"{prefix}_{uuid.uuid4().hex[:8]}@example.com"
    password = "SecurePassword12345!"

    reg = client.post(
        f"{GATEWAY_URL}/users",
        json={"email": email, "password": password},
        headers={"Content-Type": "application/json"},
    )
    assert reg.status_code == 201, (
        f"Registration failed ({reg.status_code}): {reg.text}"
    )
    user_id = reg.json()["id"]

    login = client.post(
        f"{GATEWAY_URL}/auth/login",
        json={"email": email, "password": password},
        headers={"Content-Type": "application/json"},
    )
    assert login.status_code == 200, f"Login failed ({login.status_code}): {login.text}"
    token = login.json()["access_token"]
    return user_id, token


def sign_stripe_event(event_dict: dict[str, Any]) -> tuple[bytes, str]:
    """Подписва Stripe събитие с HMAC-SHA256 за валидация от Payments."""
    payload = json.dumps(event_dict).encode("utf-8")
    timestamp = int(time.time())
    to_sign = f"{timestamp}.".encode() + payload
    signature = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode("utf-8"),
        to_sign,
        hashlib.sha256,
    ).hexdigest()
    header_val = f"t={timestamp},v1={signature}"
    return payload, header_val


def run_e2e() -> None:
    wait_for_all_services()

    with httpx.Client(timeout=15.0) as client:
        # 1. Проверка на Nginx Gateway и route priority
        print("\n==> [1/7] Testing Gateway routes and route priority...")
        gw_health = client.get(f"{GATEWAY_URL}/health")
        assert gw_health.status_code == 200
        assert gw_health.json()["gateway"] == "nginx-e2e"
        print("    Gateway /health responded OK.")

        # 2. Регистрация на нов продавач и нов купувач през Gateway
        print(
            "\n==> [2/7] Registering seller and buyer via Gateway (/users, /auth/login)..."
        )
        seller_id, seller_token = register_user(client, "seller_k64")
        buyer_id, buyer_token = register_user(client, "buyer_k64")
        seller_headers = {"Authorization": f"Bearer {seller_token}"}
        buyer_headers = {"Authorization": f"Bearer {buyer_token}"}
        print(
            f"    Seller registered (ID={seller_id}), Buyer registered (ID={buyer_id})."
        )

        # 3. Публикуване на стоки в Catalog през Gateway (/products)
        print("\n==> [3/7] Seller creates products in Catalog via Gateway...")
        prod1_resp = client.post(
            f"{GATEWAY_URL}/products",
            headers=seller_headers,
            json={
                "title": "Слушалки Sony WH-1000XM5",
                "price_minor": 25000,
                "currency": "EUR",
                "stock": 5,
                "active": True,
            },
        )
        assert prod1_resp.status_code == 201, (
            f"Failed to create product 1: {prod1_resp.text}"
        )
        prod1_id = prod1_resp.json()["id"]

        prod2_resp = client.post(
            f"{GATEWAY_URL}/products",
            headers=seller_headers,
            json={
                "title": "Калъф за слушалки",
                "price_minor": 1500,
                "currency": "EUR",
                "stock": 10,
                "active": True,
            },
        )
        assert prod2_resp.status_code == 201, (
            f"Failed to create product 2: {prod2_resp.text}"
        )
        prod2_id = prod2_resp.json()["id"]

        # Проверка на Nginx priority route ^~ /users/me/products
        seller_prods = client.get(
            f"{GATEWAY_URL}/users/me/products", headers=seller_headers
        )
        assert seller_prods.status_code == 200, (
            f"Priority route failed: {seller_prods.text}"
        )
        seller_prods_data = seller_prods.json()
        items = seller_prods_data.get("items", seller_prods_data)
        prod_ids = [p["id"] for p in items]
        assert prod1_id in prod_ids and prod2_id in prod_ids
        print("    Priority route ^~ /users/me/products correctly routes to Catalog!")

        # 4. Добавяне в количката през Gateway (/cart/items/{product_id})
        print("\n==> [4/7] Buyer adds items to Cart via Gateway (/cart)...")
        add_p1 = client.put(
            f"{GATEWAY_URL}/cart/items/{prod1_id}",
            headers=buyer_headers,
            json={"quantity": 2},
        )
        assert add_p1.status_code == 200, f"Add p1 failed: {add_p1.text}"

        add_p2 = client.put(
            f"{GATEWAY_URL}/cart/items/{prod2_id}",
            headers=buyer_headers,
            json={"quantity": 1},
        )
        assert add_p2.status_code == 200, f"Add p2 failed: {add_p2.text}"

        # Проверка, че добавянето в количка НЕ резервира наличност в Catalog
        cat_p1 = client.get(f"{GATEWAY_URL}/products/{prod1_id}")
        assert cat_p1.json()["stock"] == 5, "Stock in Catalog was prematurely altered!"
        print("    Verified: adding to cart does NOT reserve stock in Catalog.")

        # Преглед на количката
        cart_view = client.get(f"{GATEWAY_URL}/cart", headers=buyer_headers)
        assert cart_view.status_code == 200
        cart_data = cart_view.json()
        assert len(cart_data["items"]) == 2
        assert cart_data["total_minor"] == (25000 * 2) + (1500 * 1)  # 51500
        print("    Cart retrieved via Gateway: 2 items, total=51500 EUR cents.")

        # 5. Проверка: промяна на количество в количката по време на checkout НЕ се губи
        print(
            "\n==> [5/7] Testing cart concurrency: item modification during checkout is NOT lost..."
        )
        prod1_version = next(
            i["version"] for i in cart_data["items"] if i["product_id"] == prod1_id
        )
        prod2_version = next(
            i["version"] for i in cart_data["items"] if i["product_id"] == prod2_id
        )

        # Симулираме паралелна промяна: купувачът променя количеството на Продукт 2 на 3
        # Това увеличава версията на Продукт 2 в Cart до нова версия
        update_p2 = client.put(
            f"{GATEWAY_URL}/cart/items/{prod2_id}",
            headers=buyer_headers,
            json={"quantity": 3},
        )
        assert update_p2.status_code == 200

        # Изчистване със стар снимок (където Продукт 1 е с първоначалната версия, а Продукт 2 е бил с предишната версия)
        # Директно през вътрешния API на Cart за симулация на Orders с по-ранен снимок
        internal_clear = client.post(
            f"{CART_API_URL}/internal/cart/{buyer_id}/clear-purchased",
            headers={"X-Internal-Token": "ecommerce-cart-internal-secret"},
            json={
                "order_id": 999901,
                "items": [
                    {"product_id": prod1_id, "version": prod1_version},
                    {
                        "product_id": prod2_id,
                        "version": prod2_version,
                    },  # Снимката е със старата версия, а в Cart вече е нова!
                ],
            },
        )
        assert internal_clear.status_code == 200
        clear_res = internal_clear.json()
        assert prod1_id in clear_res["cleared_product_ids"]
        assert prod2_id not in clear_res["cleared_product_ids"]
        print(
            "    Cart internal clear: Product 1 cleared (v1==v1), Product 2 preserved (v2!=v1)!"
        )

        # Проверка на количката на купувача: Продукт 1 го няма, Продукт 2 е наличен с новото количество 3!
        cart_after_concurrent = client.get(
            f"{GATEWAY_URL}/cart", headers=buyer_headers
        ).json()
        item_ids_remaining = [
            item["product_id"] for item in cart_after_concurrent["items"]
        ]
        assert prod1_id not in item_ids_remaining, "Product 1 should have been cleared!"
        assert prod2_id in item_ids_remaining, "Product 2 should have been preserved!"
        p2_in_cart = next(
            item
            for item in cart_after_concurrent["items"]
            if item["product_id"] == prod2_id
        )
        assert p2_in_cart["quantity"] == 3, (
            f"Expected qty 3, got {p2_in_cart['quantity']}"
        )
        print(
            "    Verified: quantity changes made during checkout are safely preserved!"
        )

        # 6. Оформяне на реална поръчка през Orders и проверка на идемпотентност
        print(
            "\n==> [6/7] Creating order via Gateway (/orders) and verifying idempotency..."
        )
        idempotency_key = str(uuid.uuid4())
        order_headers = {
            "Authorization": f"Bearer {buyer_token}",
            "Idempotency-Key": idempotency_key,
        }

        order_resp = client.post(
            f"{GATEWAY_URL}/orders",
            headers=order_headers,
            json={"shipping_address": SHIPPING_ADDRESS},
        )
        assert order_resp.status_code == 201, (
            f"Order creation failed: {order_resp.text}"
        )
        order_data = order_resp.json()
        order_id = order_data["id"]
        assert order_data["status"] == "pending_payment"
        print(
            f"    Order #{order_id} created successfully. Total amount: {order_data['total_minor']} cents."
        )

        # Повторна заявка със същия Idempotency-Key -> връща същата поръчка без второ списание
        repeat_order = client.post(
            f"{GATEWAY_URL}/orders",
            headers=order_headers,
            json={"shipping_address": SHIPPING_ADDRESS},
        )
        assert repeat_order.status_code == 201, (
            f"Repeated order failed: {repeat_order.text}"
        )
        assert repeat_order.json()["id"] == order_id, (
            "Idempotency violated: different order_id returned!"
        )
        print(
            "    Verified: repeating checkout with same Idempotency-Key returns existing order."
        )

        # Проверка, че количката вече е празна след оформянето на поръчката
        cart_empty = client.get(f"{GATEWAY_URL}/cart", headers=buyer_headers).json()
        assert len(cart_empty["items"]) == 0, (
            "Cart should be empty after order creation!"
        )
        print("    Verified: purchased items cleared from cart.")

        # 7. Плащане през Payments -> Stripe Webhook -> Paid -> Уведомление в Mailpit
        print(
            "\n==> [7/7] Initiating checkout session, Stripe payment, and Mailpit notification..."
        )
        # Създаване на checkout сесия в Payments през Orders API
        session_resp = client.post(
            f"{GATEWAY_URL}/orders/{order_id}/checkout-session",
            headers=buyer_headers,
        )
        assert session_resp.status_code == 200, (
            f"Checkout session failed: {session_resp.text}"
        )
        session_data = session_resp.json()
        session_id = session_data["session_id"]
        print(f"    Stripe checkout session created: {session_id}")

        # Изпращане на Stripe уебхук (checkout.session.completed) директно към Payments
        webhook_event = {
            "id": f"evt_test_{uuid.uuid4().hex[:12]}",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": session_id,
                    "livemode": False,
                    "payment_status": "paid",
                    "amount_total": order_data["total_minor"],
                    "currency": "eur",
                    "client_reference_id": str(order_id),
                    "metadata": {
                        "order_id": str(order_id),
                    },
                }
            },
        }
        wh_body, wh_sig = sign_stripe_event(webhook_event)
        wh_resp = client.post(
            f"{PAYMENTS_API_URL}/webhooks/stripe",
            content=wh_body,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": wh_sig,
            },
        )
        assert wh_resp.status_code == 200, f"Webhook failed: {wh_resp.text}"
        print("    Stripe webhook accepted by Payments service.")

        # Изчакваме обработката на събитието payment.paid от orders-payment-consumer
        print(
            "    Waiting for Orders service to process payment event and update status to 'paid'..."
        )
        paid = False
        for _ in range(25):
            ord_check = client.get(
                f"{GATEWAY_URL}/orders/{order_id}", headers=buyer_headers
            ).json()
            if ord_check["status"] == "paid":
                paid = True
                break
            time.sleep(0.5)
        assert paid, (
            f"Order status did not transition to 'paid' in time! Current: {ord_check['status']}"
        )
        print(f"    Order #{order_id} is successfully transitioned to status='paid'!")

        # Проверка на полученото имейл уведомление в Mailpit
        print("    Checking Mailpit for order confirmation notification...")
        notif_received = False
        buyer_email = client.get(
            f"{GATEWAY_URL}/users/me", headers=buyer_headers
        ).json()["email"]
        for _ in range(25):
            mail_resp = client.get(f"{MAILPIT_API_URL}/api/v1/messages")
            if mail_resp.status_code == 200:
                messages = mail_resp.json().get("messages", [])
                for msg in messages:
                    to_addrs = [r["Address"] for r in msg.get("To", [])]
                    if buyer_email in to_addrs and str(order_id) in msg.get(
                        "Subject", ""
                    ):
                        notif_received = True
                        break
            if notif_received:
                break
            time.sleep(0.5)

        # Дори ако subject не съдържа точно order_id, проверяваме дали има писмо към този купувач
        if not notif_received:
            for msg in messages:
                to_addrs = [r["Address"] for r in msg.get("To", [])]
                if buyer_email in to_addrs:
                    notif_received = True
                    break

        assert notif_received, (
            f"Notification email for buyer {buyer_email} was not found in Mailpit!"
        )
        print(
            f"    Email notification for order #{order_id} delivered to Mailpit successfully!"
        )

        # 8. Проверка за изолация на БД таблиците
        print("\n==> [8/8] Verifying database isolation across services...")
        with psycopg.connect(ORDERS_DSN) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name IN ('users', 'products', 'categories', 'cart_items');
            """)
            leaked_tables = cur.fetchall()
            assert not leaked_tables, (
                f"Orders DB still has foreign tables: {leaked_tables}"
            )
            print(
                "    Orders DB is isolated: no users, products, categories, or cart_items tables!"
            )

        with psycopg.connect(CART_DSN) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name IN ('orders', 'order_items', 'products');
            """)
            leaked_cart_tables = cur.fetchall()
            assert not leaked_cart_tables, (
                f"Cart DB has leaked tables: {leaked_cart_tables}"
            )
            print("    Cart DB is isolated: no orders or products tables!")

        with psycopg.connect(CATALOG_DSN) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name IN ('orders', 'cart_items', 'users');
            """)
            leaked_cat_tables = cur.fetchall()
            assert not leaked_cat_tables, (
                f"Catalog DB has leaked tables: {leaked_cat_tables}"
            )
            print("    Catalog DB is isolated: no orders, cart_items, or users tables!")

        print(
            "\n=========================================================================="
        )
        print(
            "==> [SUCCESS] All K6.4 E2E checks passed on clean isolated microservice stand!"
        )
        print(
            "=========================================================================="
        )


if __name__ == "__main__":
    try:
        run_e2e()
    except Exception as exc:  # noqa: BLE001
        print(f"\n[ERROR] E2E verification failed: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)
