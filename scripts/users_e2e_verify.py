"""Сквозно E2E доказателство за реална връзка Commerce -> Users в Docker (без подмени).

Сценарий:
1. Регистрира нов купувач през публичния Commerce адрес (POST /users) -> 201
2. Изпълнява вход през публичния Commerce адрес (POST /auth/login) -> 200
3. С получения токен извиква GET /cart през Commerce -> 200
4. Проверява в PostgreSQL:
   - акаунтът съществува в базата данни на Users (users.users);
   - акаунтът отсъства в историческата таблица на Commerce (commerce.users).
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

    buyer_email = f"e2e_buyer_{uuid.uuid4().hex[:10]}@example.com"
    buyer_password = "SecurePassword123!"

    print(f"==> Step 1: POST {COMMERCE_API_URL}/users (register new buyer: {buyer_email})")
    reg_res = httpx.post(
        f"{COMMERCE_API_URL}/users",
        json={"email": buyer_email, "password": buyer_password},
        timeout=10.0,
    )
    print(f"    Status: {reg_res.status_code}, Response: {reg_res.text}")
    assert reg_res.status_code == 201, f"Expected 201 Created, got {reg_res.status_code}: {reg_res.text}"
    user_data = reg_res.json()
    new_user_id = user_data["id"]
    print(f"    Created User ID: {new_user_id}")

    print(f"==> Step 2: POST {COMMERCE_API_URL}/auth/login (authenticate buyer)")
    login_res = httpx.post(
        f"{COMMERCE_API_URL}/auth/login",
        json={"email": buyer_email, "password": buyer_password},
        timeout=10.0,
    )
    print(f"    Status: {login_res.status_code}")
    assert login_res.status_code == 200, f"Expected 200 OK, got {login_res.status_code}: {login_res.text}"
    token_data = login_res.json()
    assert "access_token" in token_data, f"access_token missing in response: {token_data}"
    access_token = token_data["access_token"]
    print("    Access token successfully obtained.")

    print(f"==> Step 3: GET {COMMERCE_API_URL}/cart with Bearer token")
    cart_res = httpx.get(
        f"{COMMERCE_API_URL}/cart",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10.0,
    )
    print(f"    Status: {cart_res.status_code}, Response: {cart_res.text}")
    assert cart_res.status_code == 200, f"Expected 200 OK, got {cart_res.status_code}: {cart_res.text}"
    cart_json = cart_res.json()
    assert "items" in cart_json, f"'items' field missing in cart response: {cart_json}"
    print(f"    Cart retrieved successfully (items count: {len(cart_json['items'])}).")

    print("==> Step 4: Verify databases directly via PostgreSQL")
    # 4.1 Проверка в базата на Users: акаунтът задължително съществува
    with psycopg.connect(USERS_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, status, role FROM users WHERE email = %s",
            (buyer_email,),
        )
        users_row = cur.fetchone()
        assert users_row is not None, f"User {buyer_email} NOT found in Users database!"
        assert users_row[0] == new_user_id
        assert users_row[1] == buyer_email
        assert users_row[2] == "active"
        print(f"    Users DB check: User found (id={users_row[0]}, email={users_row[1]}, status={users_row[2]}).")

    # 4.2 Проверка в базата на Commerce: акаунтът задължително ЛИПСВА в commerce.users
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM users WHERE email = %s",
            (buyer_email,),
        )
        commerce_row = cur.fetchone()
        assert commerce_row is None, f"Shadow row found in commerce.users for {buyer_email}: {commerce_row}!"
        print(f"    Commerce DB check: User {buyer_email} is ABSENT from commerce.users (no shadow row).")

    print("\n✅ SUCCESS: 201 -> 200 -> 200 verified with real Commerce -> Users in Docker!")


if __name__ == "__main__":
    main()

