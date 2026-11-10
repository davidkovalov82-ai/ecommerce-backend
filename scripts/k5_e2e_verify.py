"""Изолирана E2E проверка на К5 върху реални контейнери (Commerce, Payments, Notifications, Stripe Mock, PostgreSQL, RabbitMQ)."""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pika
import psycopg

ROOT_DIR = Path(__file__).resolve().parent.parent
COMPOSE_PROJECT = "commerce-k5-e2e"
COMPOSE_FILE = str(ROOT_DIR / "compose.e2e.yaml")

COMMERCE_API_URL = "http://127.0.0.1:58000"
PAYMENTS_API_URL = "http://127.0.0.1:58001"
MAILPIT_API_URL = "http://127.0.0.1:58025"
SMS_MOCK_URL = "http://127.0.0.1:58080"
STRIPE_MOCK_URL = "http://127.0.0.1:58081"
RABBITMQ_MGMT_URL = "http://127.0.0.1:55673"

INTERNAL_TOKEN = "k5-e2e-internal-payments-token-32b!"
STRIPE_WEBHOOK_SECRET = "whsec_k4_e2e_secret_stripe_commerce_32_bytes_ok"

COMMERCE_DSN = "postgresql://commerce_app:commerce_app@127.0.0.1:55432/commerce"
PAYMENTS_DSN = "postgresql://payments_app:payments_app@127.0.0.1:55432/payments"
NOTIFICATIONS_DSN = (
    "postgresql://notifications_app:notifications_app@127.0.0.1:55432/notifications"
)
ADMIN_COMMERCE_DSN = "postgresql://pgadmin:pgadmin_e2e_password@db-e2e:5432/commerce"
CONTAINER_PAYMENTS_DSN = "postgresql://payments_app:payments_app@db-e2e:5432/payments"

DEFAULT_ADDRESS = {
    "recipient_name": "Мария Георгиева",
    "phone": "+359889123456",
    "country_code": "BG",
    "city": "Sofia",
    "postal_code": "1000",
    "address_line1": "bul. Vitosha 42",
}


def wait_until[T](
    description: str,
    predicate: Callable[[], T | None],
    *,
    timeout_seconds: float = 40.0,
    poll_interval: float = 0.25,
) -> T:
    """Изчаква условие с краен таймаут."""
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(poll_interval)
    suffix = f" (last error: {last_error})" if last_error else ""
    raise AssertionError(f"Timeout waiting for: {description}{suffix}")


def compose_cmd(
    *args: str, capture_output: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", COMPOSE_FILE, *args],
        cwd=ROOT_DIR,
        capture_output=capture_output,
        text=True,
        check=True,
    )


def register_and_login(
    email: str, password: str = "VeryStrongPassword123!"
) -> tuple[int, str]:
    reg = httpx.post(
        f"{COMMERCE_API_URL}/users",
        json={"email": email, "password": password},
        timeout=5.0,
    )
    assert reg.status_code == 201, f"Register failed: {reg.text}"
    user_id = reg.json()["id"]

    login = httpx.post(
        f"{COMMERCE_API_URL}/auth/login",
        json={"email": email, "password": password},
        timeout=5.0,
    )
    assert login.status_code == 200, f"Login failed: {login.text}"
    return int(user_id), login.json()["access_token"]


def create_product(token: str, *, title: str, price_minor: int, stock: int = 10) -> int:
    resp = httpx.post(
        f"{COMMERCE_API_URL}/products",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "title": title,
            "price_minor": price_minor,
            "currency": "EUR",
            "stock": stock,
            "active": True,
        },
        timeout=5.0,
    )
    assert resp.status_code == 201, f"Product create failed: {resp.text}"
    return int(resp.json()["id"])


def get_product_stock(product_id: int) -> int:
    resp = httpx.get(f"{COMMERCE_API_URL}/products/{product_id}", timeout=5.0)
    assert resp.status_code == 200, f"Get product failed: {resp.text}"
    return int(resp.json()["stock"])


def add_to_cart(token: str, product_id: int, quantity: int) -> None:
    resp = httpx.put(
        f"{COMMERCE_API_URL}/cart/items/{product_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"quantity": quantity},
        timeout=5.0,
    )
    assert resp.status_code == 200, f"Add to cart failed: {resp.text}"


def create_order(token: str, *, phone: str | None = None) -> dict[str, Any]:
    address = dict(DEFAULT_ADDRESS)
    if phone is not None:
        address["phone"] = phone
    resp = httpx.post(
        f"{COMMERCE_API_URL}/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
        json={"shipping_address": address},
        timeout=5.0,
    )
    assert resp.status_code == 201, f"Order create failed: {resp.text}"
    return dict(resp.json())


def get_order_detail(token: str, order_id: int) -> dict[str, Any]:
    resp = httpx.get(
        f"{COMMERCE_API_URL}/orders/{order_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5.0,
    )
    assert resp.status_code == 200, f"Get order failed: {resp.text}"
    return dict(resp.json())


def sign_and_post_stripe_webhook(event_payload: dict[str, Any]) -> httpx.Response:
    payload_bytes = json.dumps(event_payload).encode("utf-8")
    ts = int(time.time())
    signed = f"{ts}.".encode() + payload_bytes
    sig = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode("utf-8"),
        signed,
        hashlib.sha256,
    ).hexdigest()
    return httpx.post(
        f"{PAYMENTS_API_URL}/webhooks/stripe",
        content=payload_bytes,
        headers={"Stripe-Signature": f"t={ts},v1={sig}"},
        timeout=5.0,
    )


def get_mailpit_messages(recipient_email: str) -> list[dict[str, Any]]:
    resp = httpx.get(f"{MAILPIT_API_URL}/api/v1/messages", timeout=3.0)
    resp.raise_for_status()
    return [
        m
        for m in resp.json().get("messages", [])
        if any(r.get("Address") == recipient_email for r in m.get("To", []))
    ]


def get_sms_sent_for_phone(phone: str) -> list[dict[str, Any]]:
    resp = httpx.get(f"{SMS_MOCK_URL}/_sent", timeout=3.0)
    resp.raise_for_status()
    return [m for m in resp.json().get("messages", []) if m.get("to") == phone]


def get_commerce_outbox_rows(order_id: int) -> list[dict[str, Any]]:
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_id, event_type, status, retry_count FROM order_outbox_events WHERE order_id = %s ORDER BY id",
            (order_id,),
        )
        return [
            {
                "event_id": r[0],
                "event_type": r[1],
                "status": r[2],
                "retry_count": r[3],
            }
            for r in cur.fetchall()
        ]


def get_payment_outbox_rows(order_id: int) -> list[dict[str, Any]]:
    with psycopg.connect(PAYMENTS_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_id, event_type, status, retry_count FROM payment_outbox_events WHERE order_id = %s ORDER BY id",
            (order_id,),
        )
        return [
            {
                "event_id": r[0],
                "event_type": r[1],
                "status": r[2],
                "retry_count": r[3],
            }
            for r in cur.fetchall()
        ]


def get_commerce_inbox_rows(order_id: int) -> list[dict[str, Any]]:
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_id, event_type, outcome FROM order_inbox_events WHERE order_id = %s",
            (order_id,),
        )
        return [
            {"event_id": r[0], "event_type": r[1], "outcome": r[2]}
            for r in cur.fetchall()
        ]


def get_rabbitmq_queue_count(queue_name: str) -> int:
    resp = httpx.get(
        f"{RABBITMQ_MGMT_URL}/api/queues/%2F/{queue_name}",
        auth=("guest", "guest"),
        timeout=3.0,
    )
    if resp.status_code == 404:
        return 0
    resp.raise_for_status()
    return int(resp.json().get("messages", 0))


def _assert_db_access_denied(dsn: str) -> None:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            raise AssertionError(f"Expected permission denied for {dsn}, connected!")
    except psycopg.OperationalError as exc:
        msg = str(exc).lower()
        sqlstate = getattr(exc, "sqlstate", None)
        assert sqlstate == "42501" or "permission denied for database" in msg, (
            f"Expected PostgreSQL permission denied (42501), got: {exc!r}"
        )


def _get_container_state(service_name: str) -> dict[str, Any]:
    cid = compose_cmd("ps", "-q", service_name, capture_output=True).stdout.strip()
    assert cid, f"Container not found for {service_name}"
    insp = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.Id}}|{{.State.Pid}}|{{.RestartCount}}|{{json .Config.Env}}",
            cid,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    cid_full, pid, restart_count, env_json = insp.stdout.strip().split("|", 3)
    return {
        "id": cid_full,
        "pid": int(pid),
        "restart_count": int(restart_count),
        "env": list(json.loads(env_json)),
    }


def verify_boundaries_and_isolation() -> None:
    print("-> [K5-1] 3-way DB isolation, secret separation & internal auth boundary")
    # 1. PostgreSQL role isolation across all 3 services
    for dsn, table in (
        (COMMERCE_DSN, "order_outbox_events"),
        (PAYMENTS_DSN, "payment_attempts"),
        (NOTIFICATIONS_DSN, "inbox_events"),
    ):
        with psycopg.connect(dsn, connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            assert cur.fetchone()[0] is False
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            assert cur.fetchone() is not None

    for role_user, target_db in (
        ("payments_app", "commerce"),
        ("payments_app", "notifications"),
        ("commerce_app", "payments"),
        ("commerce_app", "notifications"),
        ("notifications_app", "payments"),
        ("notifications_app", "commerce"),
    ):
        _assert_db_access_denied(
            f"postgresql://{role_user}:{role_user}@127.0.0.1:55432/{target_db}"
        )

    # 2. Zero STRIPE_* variables in Commerce containers
    for svc in (
        "commerce-api-e2e",
        "commerce-outbox-e2e",
        "commerce-payment-consumer-e2e",
    ):
        state = _get_container_state(svc)
        stripe_vars = [e for e in state["env"] if e.startswith("STRIPE_")]
        assert not stripe_vars, (
            f"Commerce service {svc} leaked STRIPE env: {stripe_vars}"
        )

    # 3. Commerce no longer exposes /webhooks/stripe
    r_old_wh = httpx.post(
        f"{COMMERCE_API_URL}/webhooks/stripe", content=b"{}", timeout=5.0
    )
    assert r_old_wh.status_code == 404

    # 4. Payments internal endpoints reject unauthenticated / wrong token calls
    for url, body in (
        (
            f"{PAYMENTS_API_URL}/internal/payments/checkout-sessions",
            {
                "order_id": 1,
                "attempt_key": "k",
                "amount_minor": 1000,
                "currency": "EUR",
                "line_items": [],
            },
        ),
        (
            f"{PAYMENTS_API_URL}/internal/payments/orders/1/cancel",
            {"reason": "cancel"},
        ),
    ):
        assert httpx.post(url, json=body, timeout=5.0).status_code == 401
        assert (
            httpx.post(
                url,
                json=body,
                headers={"X-Internal-Token": "wrong-token"},
                timeout=5.0,
            ).status_code
            == 401
        )


def verify_concurrent_and_lost_stripe_response(seller_token: str) -> None:
    print("-> [K5-2] Concurrent checkout sessions & lost Stripe response recovery")
    httpx.post(f"{STRIPE_MOCK_URL}/_reset", timeout=5.0).raise_for_status()

    _, buyer_token = register_and_login(f"k5_race_{uuid.uuid4().hex[:8]}@example.com")
    pid = create_product(
        seller_token, title="K5 Race Product", price_minor=2500, stock=10
    )
    add_to_cart(buyer_token, pid, 2)  # 5000 minor
    order = create_order(buyer_token)
    oid = int(order["id"])

    def _call_checkout() -> tuple[int, dict[str, Any]]:
        r = httpx.post(
            f"{COMMERCE_API_URL}/orders/{oid}/checkout-session",
            headers={"Authorization": f"Bearer {buyer_token}"},
            timeout=10.0,
        )
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _call_checkout(), range(2)))

    assert all(code == 200 for code, _ in results), f"Expected 200s, got {results}"
    assert results[0][1]["session_id"] == results[1][1]["session_id"]
    mock_state = httpx.get(f"{STRIPE_MOCK_URL}/_sessions", timeout=5.0).json()
    assert len(mock_state["sessions"]) == 1
    assert mock_state["sessions"][0]["amount_total"] == 5000

    # Загубен отговор след създаване в Stripe -> повторен опит възстановява същата сесия
    add_to_cart(buyer_token, pid, 1)
    order2 = create_order(buyer_token)
    oid2 = int(order2["id"])

    httpx.post(
        f"{STRIPE_MOCK_URL}/_control",
        json={"drop_next_create_after_commit": True},
        timeout=5.0,
    ).raise_for_status()

    first_fail = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid2}/checkout-session",
        headers={"Authorization": f"Bearer {buyer_token}"},
        timeout=10.0,
    )
    assert first_fail.status_code == 502, (
        f"Expected 502 on lost response, got {first_fail.text}"
    )

    sessions_after_drop = httpx.get(f"{STRIPE_MOCK_URL}/_sessions", timeout=5.0).json()[
        "sessions"
    ]
    created_for_o2 = [
        s for s in sessions_after_drop if s["client_reference_id"] == str(oid2)
    ]
    assert len(created_for_o2) == 1
    expected_recovered_id = created_for_o2[0]["id"]

    recovered = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid2}/checkout-session",
        headers={"Authorization": f"Bearer {buyer_token}"},
        timeout=10.0,
    )
    assert recovered.status_code == 200, f"Recovery failed: {recovered.text}"
    assert recovered.json()["session_id"] == expected_recovered_id
    sessions_after_retry = httpx.get(
        f"{STRIPE_MOCK_URL}/_sessions", timeout=5.0
    ).json()["sessions"]
    assert (
        len([s for s in sessions_after_retry if s["client_reference_id"] == str(oid2)])
        == 1
    )


def verify_commerce_offline_during_webhook_then_recovery(seller_token: str) -> None:
    print(
        "-> [K5-3] Commerce stopped -> Stripe webhook accepted by Payments -> Commerce recovers -> paid once & 1 notification"
    )
    email = f"k5_offline_{uuid.uuid4().hex[:8]}@example.com"
    phone = f"+359887{uuid.uuid4().int % 1000000:06d}"
    _, buyer_token = register_and_login(email)
    pid = create_product(
        seller_token, title="K5 Offline Product", price_minor=3000, stock=10
    )
    add_to_cart(buyer_token, pid, 2)  # stock=8, total=6000
    order = create_order(buyer_token, phone=phone)
    oid = int(order["id"])

    co = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid}/checkout-session",
        headers={"Authorization": f"Bearer {buyer_token}"},
        timeout=5.0,
    )
    assert co.status_code == 200
    session_id = co.json()["session_id"]

    # Спираме всички Commerce контейнери по време на плащането!
    compose_cmd(
        "stop",
        "commerce-api-e2e",
        "commerce-outbox-e2e",
        "commerce-payment-consumer-e2e",
    )
    try:
        wh_payload = {
            "id": f"evt_offline_{uuid.uuid4().hex[:14]}",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": session_id,
                    "livemode": False,
                    "payment_status": "paid",
                    "amount_total": 6000,
                    "currency": "eur",
                    "client_reference_id": str(oid),
                    "metadata": {
                        "order_id": str(oid),
                        "attempt_key": f"order_{oid}_attempt_1",
                    },
                }
            },
        }
        r_wh1 = sign_and_post_stripe_webhook(wh_payload)
        assert r_wh1.status_code == 200, f"Offline webhook failed: {r_wh1.text}"
        assert r_wh1.json()["status"] == "success"

        # Идемпотентен повторен webhook + второ event ID -> без повторно списание / излишно outbox събитие
        r_wh_dup = sign_and_post_stripe_webhook(wh_payload)
        assert r_wh_dup.status_code == 200
        assert r_wh_dup.json()["status"] == "already_processed"

        wh_payload2 = dict(wh_payload, id=f"evt_offline_second_{uuid.uuid4().hex[:10]}")
        r_wh2 = sign_and_post_stripe_webhook(wh_payload2)
        assert r_wh2.status_code == 200

        with psycopg.connect(PAYMENTS_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM payment_attempts WHERE order_id = %s",
                (oid,),
            )
            assert cur.fetchall() == [("succeeded",)]

        wait_until(
            "Payments outbox published payment.succeeded while Commerce down",
            lambda: (
                rows
                if (rows := get_payment_outbox_rows(oid))
                and len(rows) == 1
                and rows[0]["status"] == "published"
                else None
            ),
        )
    finally:
        compose_cmd(
            "start",
            "commerce-api-e2e",
            "commerce-outbox-e2e",
            "commerce-payment-consumer-e2e",
        )

    wait_until(
        "Commerce API recovered",
        lambda: (
            True
            if httpx.get(f"{COMMERCE_API_URL}/openapi.json", timeout=2.0).status_code
            == 200
            else None
        ),
    )

    wait_until(
        "Commerce order became paid after recovery",
        lambda: (
            True if get_order_detail(buyer_token, oid)["status"] == "paid" else None
        ),
    )
    assert get_product_stock(pid) == 8

    outbox_rows = wait_until(
        "Commerce order.paid outbox published once",
        lambda: (
            rows
            if (rows := get_commerce_outbox_rows(oid))
            and len(rows) == 1
            and rows[0]["status"] == "published"
            else None
        ),
    )
    assert len(outbox_rows) == 1

    wait_until(
        "Offline recovery delivered exactly 1 email",
        lambda: m if len(m := get_mailpit_messages(email)) == 1 else None,
    )
    wait_until(
        "Offline recovery delivered exactly 1 SMS",
        lambda: s if len(s := get_sms_sent_for_phone(phone)) == 1 else None,
    )


def verify_rabbitmq_down_during_payment_recovery(seller_token: str) -> None:
    print(
        "-> [K5-4] RabbitMQ down during webhook -> stays pending in Payments Outbox -> recovers -> paid & notified"
    )
    email = f"k5_rmq_{uuid.uuid4().hex[:8]}@example.com"
    phone = f"+359886{uuid.uuid4().int % 1000000:06d}"
    _, buyer_token = register_and_login(email)
    pid = create_product(seller_token, title="K5 RMQ Item", price_minor=1800, stock=5)
    add_to_cart(buyer_token, pid, 1)
    order = create_order(buyer_token, phone=phone)
    oid = int(order["id"])

    co = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid}/checkout-session",
        headers={"Authorization": f"Bearer {buyer_token}"},
        timeout=5.0,
    )
    assert co.status_code == 200
    session_id = co.json()["session_id"]

    compose_cmd("stop", "rabbitmq-e2e")
    try:
        wh = sign_and_post_stripe_webhook(
            {
                "id": f"evt_rmq_{uuid.uuid4().hex[:14]}",
                "type": "checkout.session.completed",
                "livemode": False,
                "data": {
                    "object": {
                        "id": session_id,
                        "livemode": False,
                        "payment_status": "paid",
                        "amount_total": 1800,
                        "currency": "eur",
                        "client_reference_id": str(oid),
                        "metadata": {
                            "order_id": str(oid),
                            "attempt_key": f"order_{oid}_attempt_1",
                        },
                    }
                },
            }
        )
        assert wh.status_code == 200
        wait_until(
            "Payments outbox retry_count >= 1 while RabbitMQ down",
            lambda: (
                rows[0]
                if (rows := get_payment_outbox_rows(oid))
                and rows[0]["status"] == "pending"
                and rows[0]["retry_count"] >= 1
                else None
            ),
        )
        assert get_order_detail(buyer_token, oid)["status"] == "pending_payment"
    finally:
        compose_cmd("start", "rabbitmq-e2e")

    wait_until(
        "Order paid after RabbitMQ recovery",
        lambda: (
            True if get_order_detail(buyer_token, oid)["status"] == "paid" else None
        ),
    )
    wait_until(
        "Email delivered after RabbitMQ recovery",
        lambda: m if len(m := get_mailpit_messages(email)) == 1 else None,
    )
    wait_until(
        "SMS delivered after RabbitMQ recovery",
        lambda: s if len(s := get_sms_sent_for_phone(phone)) == 1 else None,
    )


def verify_async_failure_retry_and_success(seller_token: str) -> None:
    print(
        "-> [K5-5] Delayed async payment: processing -> async_payment_failed -> retry new attempt -> async_payment_succeeded"
    )
    email = f"k5_async_{uuid.uuid4().hex[:8]}@example.com"
    phone = f"+359885{uuid.uuid4().int % 1000000:06d}"
    _, buyer_token = register_and_login(email)
    pid = create_product(
        seller_token, title="K5 Async Product", price_minor=3200, stock=5
    )
    add_to_cart(buyer_token, pid, 1)
    order = create_order(buyer_token, phone=phone)
    oid = int(order["id"])

    co1 = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid}/checkout-session",
        headers={"Authorization": f"Bearer {buyer_token}"},
        timeout=5.0,
    )
    assert co1.status_code == 200
    session1 = co1.json()["session_id"]

    # 1. Асинхронното плащане стартира (unpaid completed) -> payment_processing
    r_proc = sign_and_post_stripe_webhook(
        {
            "id": f"evt_async_proc_{uuid.uuid4().hex[:12]}",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": session1,
                    "livemode": False,
                    "payment_status": "unpaid",
                    "amount_total": 3200,
                    "currency": "eur",
                    "client_reference_id": str(oid),
                    "metadata": {
                        "order_id": str(oid),
                        "attempt_key": f"order_{oid}_attempt_1",
                    },
                }
            },
        }
    )
    assert r_proc.status_code == 200
    wait_until(
        "Order transitioned to payment_processing",
        lambda: (
            True
            if get_order_detail(buyer_token, oid)["status"] == "payment_processing"
            else None
        ),
    )

    assert (
        httpx.post(
            f"{COMMERCE_API_URL}/orders/{oid}/cancel",
            headers={"Authorization": f"Bearer {buyer_token}"},
            timeout=5.0,
        ).status_code
        == 409
    )
    assert (
        httpx.post(
            f"{COMMERCE_API_URL}/orders/{oid}/checkout-session",
            headers={"Authorization": f"Bearer {buyer_token}"},
            timeout=5.0,
        ).status_code
        == 409
    )

    # 2. Асинхронният превод се проваля -> pending_payment + attempt_2
    r_fail = sign_and_post_stripe_webhook(
        {
            "id": f"evt_async_fail_{uuid.uuid4().hex[:12]}",
            "type": "checkout.session.async_payment_failed",
            "livemode": False,
            "data": {
                "object": {
                    "id": session1,
                    "livemode": False,
                    "payment_status": "unpaid",
                    "amount_total": 3200,
                    "currency": "eur",
                    "client_reference_id": str(oid),
                    "metadata": {
                        "order_id": str(oid),
                        "attempt_key": f"order_{oid}_attempt_1",
                    },
                }
            },
        }
    )
    assert r_fail.status_code == 200

    def _has_rotated_to_attempt_2() -> bool | None:
        if get_order_detail(buyer_token, oid)["status"] != "pending_payment":
            return None
        with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT active_attempt_key FROM orders WHERE id = %s", (oid,))
            row = cur.fetchone()
            return True if row == (f"order_{oid}_attempt_2",) else None

    wait_until(
        "Order returned to pending_payment with rotated attempt_2",
        _has_rotated_to_attempt_2,
    )

    # Старият терминален ключ не може да отвори нова сесия в Payments
    r_stale = httpx.post(
        f"{PAYMENTS_API_URL}/internal/payments/checkout-sessions",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        json={
            "order_id": oid,
            "attempt_key": f"order_{oid}_attempt_1",
            "amount_minor": 3200,
            "currency": "EUR",
            "line_items": [
                {
                    "name": "K5 Async Product",
                    "unit_amount": 3200,
                    "quantity": 1,
                    "currency": "eur",
                }
            ],
        },
        timeout=5.0,
    )
    assert r_stale.status_code == 409

    # 3. Нов checkout с attempt_2 + async_payment_succeeded -> paid + notification
    co2 = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid}/checkout-session",
        headers={"Authorization": f"Bearer {buyer_token}"},
        timeout=5.0,
    )
    assert co2.status_code == 200
    session2 = co2.json()["session_id"]
    assert session2 != session1

    r_ok = sign_and_post_stripe_webhook(
        {
            "id": f"evt_async_ok_{uuid.uuid4().hex[:12]}",
            "type": "checkout.session.async_payment_succeeded",
            "livemode": False,
            "data": {
                "object": {
                    "id": session2,
                    "livemode": False,
                    "payment_status": "paid",
                    "amount_total": 3200,
                    "currency": "eur",
                    "client_reference_id": str(oid),
                    "metadata": {
                        "order_id": str(oid),
                        "attempt_key": f"order_{oid}_attempt_2",
                    },
                }
            },
        }
    )
    assert r_ok.status_code == 200

    wait_until(
        "Order paid after retry attempt_2",
        lambda: (
            True if get_order_detail(buyer_token, oid)["status"] == "paid" else None
        ),
    )
    assert get_product_stock(pid) == 4
    wait_until(
        "Retry delivered 1 email",
        lambda: m if len(m := get_mailpit_messages(email)) == 1 else None,
    )
    wait_until(
        "Retry delivered 1 SMS",
        lambda: s if len(s := get_sms_sent_for_phone(phone)) == 1 else None,
    )


def verify_cancel_and_late_payment_discrepancy(seller_token: str) -> None:
    print(
        "-> [K5-6] Cancel expires Stripe session + late payment.succeeded creates visible discrepancy without changing status/stock/outbox"
    )
    email = f"k5_disc_{uuid.uuid4().hex[:8]}@example.com"
    phone = f"+359884{uuid.uuid4().int % 1000000:06d}"
    _, buyer_token = register_and_login(email)
    pid = create_product(
        seller_token, title="K5 Disc Product", price_minor=2000, stock=10
    )
    add_to_cart(buyer_token, pid, 2)  # stock=8
    order = create_order(buyer_token, phone=phone)
    oid = int(order["id"])

    co = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid}/checkout-session",
        headers={"Authorization": f"Bearer {buyer_token}"},
        timeout=5.0,
    )
    assert co.status_code == 200
    session_id = co.json()["session_id"]

    cancel_resp = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid}/cancel",
        headers={"Authorization": f"Bearer {buyer_token}"},
        timeout=5.0,
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"
    assert get_product_stock(pid) == 10

    mock_sess = httpx.get(
        f"{STRIPE_MOCK_URL}/v1/checkout/sessions/{session_id}", timeout=5.0
    ).json()
    assert mock_sess["status"] == "expired"

    # Късно плащане след отмяната
    r_late = sign_and_post_stripe_webhook(
        {
            "id": f"evt_late_disc_{uuid.uuid4().hex[:12]}",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": session_id,
                    "livemode": False,
                    "payment_status": "paid",
                    "amount_total": 4000,
                    "currency": "eur",
                    "client_reference_id": str(oid),
                    "metadata": {
                        "order_id": str(oid),
                        "attempt_key": f"order_{oid}_attempt_1",
                    },
                }
            },
        }
    )
    assert r_late.status_code == 200

    wait_until(
        "Discrepancy recorded in Commerce inbox",
        lambda: (
            rows
            if (rows := get_commerce_inbox_rows(oid))
            and any(r["outcome"] == "discrepancy" for r in rows)
            else None
        ),
    )

    detail = get_order_detail(buyer_token, oid)
    assert detail["status"] == "cancelled"
    assert detail["has_payment_discrepancy"] is True
    assert len(detail["payment_discrepancies"]) == 1
    assert detail["payment_discrepancies"][0]["reason"] == "payment_on_cancelled_order"
    assert get_product_stock(pid) == 10
    assert get_commerce_outbox_rows(oid) == []
    assert get_mailpit_messages(email) == []
    assert get_sms_sent_for_phone(phone) == []

    # Допълнителна проверка #2: cancel без предишен опит записва closure и блокира закъснял checkout към Payments
    _, buyer_closed = register_and_login(f"k5_close_{uuid.uuid4().hex[:8]}@example.com")
    pid_close = create_product(
        seller_token, title="K5 Close Race Prod", price_minor=1500, stock=6
    )
    add_to_cart(buyer_closed, pid_close, 2)  # stock=4
    order_close = create_order(buyer_closed)
    oid_close = int(order_close["id"])

    r_close_cancel = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid_close}/cancel",
        headers={"Authorization": f"Bearer {buyer_closed}"},
        timeout=5.0,
    )
    assert r_close_cancel.status_code == 200
    assert r_close_cancel.json()["status"] == "cancelled"
    assert get_product_stock(pid_close) == 6

    r_delayed_checkout = httpx.post(
        f"{PAYMENTS_API_URL}/internal/payments/checkout-sessions",
        json={
            "order_id": oid_close,
            "attempt_key": f"order_{oid_close}_attempt_1",
            "amount_minor": 3000,
            "currency": "EUR",
            "line_items": [
                {"name": "K5 Close Race Prod", "unit_amount": 1500, "quantity": 2}
            ],
        },
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=5.0,
    )
    assert r_delayed_checkout.status_code == 409
    assert r_delayed_checkout.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"
    all_sessions = httpx.get(f"{STRIPE_MOCK_URL}/_sessions", timeout=5.0).json()[
        "sessions"
    ]
    assert not any(s.get("client_reference_id") == str(oid_close) for s in all_sessions)
    assert get_product_stock(pid_close) == 6

    # Допълнителна проверка #1 (прекъсната отмяна cancelling): блокира checkout до сверка и след reconcile_payments.py автоматично завършва в cancelled
    _, buyer_int = register_and_login(
        f"k5_int_cancel_{uuid.uuid4().hex[:8]}@example.com"
    )
    add_to_cart(buyer_int, pid_close, 2)  # stock 6 -> 4
    oid_int = int(create_order(buyer_int)["id"])
    assert get_product_stock(pid_close) == 4
    co_int = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid_int}/checkout-session",
        headers={"Authorization": f"Bearer {buyer_int}"},
        timeout=5.0,
    )
    assert co_int.status_code == 200
    sid_int = co_int.json()["session_id"]

    with psycopg.connect(PAYMENTS_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE payment_attempts SET status = 'cancelling' WHERE order_id = %s",
            (oid_int,),
        )
        cur.execute(
            "UPDATE stripe_checkout_sessions SET status = 'cancelling' WHERE order_id = %s",
            (oid_int,),
        )
        cur.execute(
            """
            INSERT INTO payment_order_closures (order_id, status, reason)
            VALUES (%s, 'cancelling', 'cancel')
            ON CONFLICT (order_id) DO UPDATE SET status = 'cancelling', reason = 'cancel'
            """,
            (oid_int,),
        )
        conn.commit()

    r_int_co_blocked = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid_int}/checkout-session",
        headers={"Authorization": f"Bearer {buyer_int}"},
        timeout=5.0,
    )
    assert r_int_co_blocked.status_code == 409
    assert get_product_stock(pid_close) == 4

    rec_int = compose_cmd(
        "exec",
        "-T",
        "payments-e2e",
        "python",
        "scripts/reconcile_payments.py",
        capture_output=True,
    )
    assert "expired=1" in rec_int.stdout
    mock_int = httpx.get(
        f"{STRIPE_MOCK_URL}/v1/checkout/sessions/{sid_int}", timeout=5.0
    ).json()
    assert mock_int["status"] == "expired"

    # БЕЗ повторен POST /cancel от клиента: Payments публикува payment.cancelled -> Commerce превежда поръчката в cancelled
    wait_until(
        "Interrupted cancel automatically completes order as cancelled via payment.cancelled",
        lambda: (
            True
            if get_order_detail(buyer_int, oid_int)["status"] == "cancelled"
            else None
        ),
    )
    assert get_product_stock(pid_close) == 6
    assert (
        httpx.post(
            f"{COMMERCE_API_URL}/orders/{oid_int}/checkout-session",
            headers={"Authorization": f"Bearer {buyer_int}"},
            timeout=5.0,
        ).status_code
        == 409
    )

    with psycopg.connect(PAYMENTS_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_id, event_type, payment_attempt_id, occurred_at, payload, status
            FROM payment_outbox_events
            WHERE order_id = %s AND event_type = 'payment.cancelled'
            ORDER BY id DESC
            LIMIT 1
            """,
            (oid_int,),
        )
        cancel_outbox_row = cur.fetchone()
    assert cancel_outbox_row is not None
    orig_cancel_eid, _, cancel_attempt_id, cancel_occ, cancel_payload, _ = (
        cancel_outbox_row
    )

    # Повторно събитие (дубликат и ново събитие) + повторна сверка не променят остатъка 6
    dup_cancel_eid = str(uuid.uuid4())
    rmq_conn = pika.BlockingConnection(
        pika.ConnectionParameters(
            host="127.0.0.1",
            port=55672,
            credentials=pika.PlainCredentials("guest", "guest"),
        )
    )
    try:
        ch = rmq_conn.channel()
        ch.confirm_delivery()
        for eid in (orig_cancel_eid, dup_cancel_eid):
            envelope = {
                "event_id": eid,
                "event_type": "payment.cancelled",
                "event_version": 1,
                "occurred_at": cancel_occ.isoformat(),
                "producer": "payments",
                "data": cancel_payload,
            }
            ch.basic_publish(
                exchange="payments.events",
                routing_key="payment.cancelled",
                body=json.dumps(envelope).encode("utf-8"),
                properties=pika.BasicProperties(delivery_mode=2),
                mandatory=True,
            )
    finally:
        rmq_conn.close()

    wait_until(
        "Repeated payment.cancelled processed idempotently in Commerce inbox",
        lambda: (
            True
            if any(
                r["event_id"] == dup_cancel_eid and r["outcome"] == "ignored"
                for r in get_commerce_inbox_rows(oid_int)
            )
            else None
        ),
    )
    assert isinstance(cancel_attempt_id, int) and cancel_attempt_id > 0

    rec_int_repeat = compose_cmd(
        "exec",
        "-T",
        "payments-e2e",
        "python",
        "scripts/reconcile_payments.py",
        capture_output=True,
    )
    assert "expired=0" in rec_int_repeat.stdout and "paid=0" in rec_int_repeat.stdout, (
        f"Unexpected repeat reconciliation stats: {rec_int_repeat.stdout}"
    )
    assert get_order_detail(buyer_int, oid_int)["status"] == "cancelled"
    assert get_product_stock(pid_close) == 6

    # Оплачена прервана отмена: Stripe вече показва complete/paid -> сверката дава paid, остатъкът остава списан
    _, buyer_int_paid = register_and_login(
        f"k5_int_paid_{uuid.uuid4().hex[:8]}@example.com"
    )
    add_to_cart(buyer_int_paid, pid_close, 1)  # stock 6 -> 5
    oid_int_paid = int(create_order(buyer_int_paid)["id"])
    co_int_paid = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid_int_paid}/checkout-session",
        headers={"Authorization": f"Bearer {buyer_int_paid}"},
        timeout=5.0,
    )
    assert co_int_paid.status_code == 200
    sid_int_paid = co_int_paid.json()["session_id"]

    with psycopg.connect(PAYMENTS_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE payment_attempts SET status = 'cancelling' WHERE order_id = %s",
            (oid_int_paid,),
        )
        cur.execute(
            "UPDATE stripe_checkout_sessions SET status = 'cancelling' WHERE order_id = %s",
            (oid_int_paid,),
        )
        cur.execute(
            """
            INSERT INTO payment_order_closures (order_id, status, reason)
            VALUES (%s, 'cancelling', 'cancel')
            ON CONFLICT (order_id) DO UPDATE SET status = 'cancelling', reason = 'cancel'
            """,
            (oid_int_paid,),
        )
        conn.commit()

    httpx.post(
        f"{STRIPE_MOCK_URL}/_control/set_session",
        json={
            "session_id": sid_int_paid,
            "status": "complete",
            "payment_status": "paid",
        },
        timeout=5.0,
    ).raise_for_status()

    rec_int_paid = compose_cmd(
        "exec",
        "-T",
        "payments-e2e",
        "python",
        "scripts/reconcile_payments.py",
        capture_output=True,
    )
    assert "paid=1" in rec_int_paid.stdout

    wait_until(
        "Paid interrupted cancel completes order as paid",
        lambda: (
            True
            if get_order_detail(buyer_int_paid, oid_int_paid)["status"] == "paid"
            else None
        ),
    )
    assert get_order_detail(buyer_int_paid, oid_int_paid)["status"] == "paid"
    assert get_product_stock(pid_close) == 5

    # Допълнителна проверка #3: отмяна ДО първи checkout при загубен HTTP отговор от Payments
    # Payments пази closure='cancelled' + payment.cancelled без фиктивна попытка/Stripe сесия -> Commerce става cancelled преди expires_at
    _, buyer_no_co = register_and_login(
        f"k5_no_attempt_can_{uuid.uuid4().hex[:8]}@example.com"
    )
    add_to_cart(buyer_no_co, pid_close, 1)  # stock 5 -> 4
    oid_no_co = int(create_order(buyer_no_co)["id"])
    assert get_product_stock(pid_close) == 4

    r_pay_no_co = httpx.post(
        f"{PAYMENTS_API_URL}/internal/payments/orders/{oid_no_co}/cancel",
        json={"reason": "cancel"},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=5.0,
    )
    assert r_pay_no_co.status_code == 200
    assert r_pay_no_co.json()["status"] == "can_cancel"

    with psycopg.connect(PAYMENTS_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM payment_attempts WHERE order_id = %s",
            (oid_no_co,),
        )
        assert cur.fetchone()[0] == 0  # type: ignore[index]
        cur.execute(
            "SELECT COUNT(*) FROM stripe_checkout_sessions WHERE order_id = %s",
            (oid_no_co,),
        )
        assert cur.fetchone()[0] == 0  # type: ignore[index]
        cur.execute(
            "SELECT status FROM payment_order_closures WHERE order_id = %s",
            (oid_no_co,),
        )
        assert cur.fetchone()[0] == "cancelled"  # type: ignore[index]

    # Без втори POST /cancel и с expires_at все още в бъдещето -> Commerce получава payment.cancelled и връща stock веднъж
    wait_until(
        "Attemptless cancellation with lost HTTP response reaches cancelled before reserve expiry",
        lambda: (
            True
            if get_order_detail(buyer_no_co, oid_no_co)["status"] == "cancelled"
            else None
        ),
    )
    assert get_product_stock(pid_close) == 5
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT payment_attempt_id, outcome FROM order_inbox_events WHERE order_id = %s AND event_type = 'payment.cancelled'",
            (oid_no_co,),
        )
        inbox_no_co = cur.fetchone()
        assert inbox_no_co == (None, "cancelled")

    # Конфликтен reserve_expired след запазено cancelled се отказва (409) без payment.expired и остава cancelled
    r_conflict_exp = httpx.post(
        f"{PAYMENTS_API_URL}/internal/payments/orders/{oid_no_co}/cancel",
        json={"reason": "reserve_expired"},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=5.0,
    )
    assert r_conflict_exp.status_code == 409
    assert r_conflict_exp.json()["detail"]["current_status"] == "cancelled"
    assert get_order_detail(buyer_no_co, oid_no_co)["status"] == "cancelled"
    assert get_product_stock(pid_close) == 5

    # Допълнителна проверка #4: Payments вече е запазил expired за поръчка с минала попытка -> идва ръчен cancel
    # Няма фалшиво payment.cancelled и крайният статус в двата сервиса е expired
    _, buyer_exp_can = register_and_login(
        f"k5_exp_then_cancel_{uuid.uuid4().hex[:8]}@example.com"
    )
    add_to_cart(buyer_exp_can, pid_close, 1)  # stock 5 -> 4
    oid_exp_can = int(create_order(buyer_exp_can)["id"])
    co_exp_can = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid_exp_can}/checkout-session",
        headers={"Authorization": f"Bearer {buyer_exp_can}"},
        timeout=5.0,
    )
    assert co_exp_can.status_code == 200
    expired_past_ts = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=25)
    ).isoformat()
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE orders SET expires_at = %s WHERE id = %s",
            (expired_past_ts, oid_exp_can),
        )
        conn.commit()
    with psycopg.connect(PAYMENTS_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE payment_attempts SET order_expires_at = %s WHERE order_id = %s",
            (expired_past_ts, oid_exp_can),
        )
        conn.commit()

    compose_cmd("stop", "payments-outbox-e2e")
    try:
        r_save_exp = httpx.post(
            f"{PAYMENTS_API_URL}/internal/payments/orders/{oid_exp_can}/cancel",
            json={"reason": "reserve_expired", "order_expires_at": expired_past_ts},
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=5.0,
        )
        assert r_save_exp.status_code == 200

        # Сега идва ръчен cancel от клиента, докато Commerce още не е обработил expired -> получава 409
        r_manual_after_exp = httpx.post(
            f"{COMMERCE_API_URL}/orders/{oid_exp_can}/cancel",
            headers={"Authorization": f"Bearer {buyer_exp_can}"},
            timeout=5.0,
        )
        assert r_manual_after_exp.status_code == 409
        assert r_manual_after_exp.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"

        outbox_exp_can = get_payment_outbox_rows(oid_exp_can)
        assert not any(
            row["event_type"] == "payment.cancelled" for row in outbox_exp_can
        )
        assert any(row["event_type"] == "payment.expired" for row in outbox_exp_can)
    finally:
        compose_cmd("start", "payments-outbox-e2e")

    wait_until(
        "Order converges to expired in Commerce after manual cancel was rejected against saved expired closure",
        lambda: (
            True
            if get_order_detail(buyer_exp_can, oid_exp_can)["status"] == "expired"
            else None
        ),
    )
    assert get_order_detail(buyer_exp_can, oid_exp_can)["status"] == "expired"
    assert get_product_stock(pid_close) == 5
    with psycopg.connect(PAYMENTS_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM payment_order_closures WHERE order_id = %s",
            (oid_exp_can,),
        )
        assert cur.fetchone()[0] == "expired"  # type: ignore[index]
        cur.execute(
            "SELECT COUNT(*) FROM payment_outbox_events WHERE order_id = %s AND event_type = 'payment.cancelled'",
            (oid_exp_can,),
        )
        assert cur.fetchone()[0] == 0  # type: ignore[index]


def verify_reconciliation_scripts(seller_token: str) -> None:
    print(
        "-> [K5-7] Scheduled reconciliation scripts: Commerce expires reserve & Payments discovers missed paid session"
    )
    # A) Просрочена поръчка -> Commerce reconcile_orders.py извиква Payments, прекратява Stripe и връща stock
    _, buyer1 = register_and_login(f"k5_rec1_{uuid.uuid4().hex[:8]}@example.com")
    pid = create_product(
        seller_token, title="K5 Rec Product", price_minor=1800, stock=10
    )
    add_to_cart(buyer1, pid, 3)  # stock=7
    o1 = create_order(buyer1)
    oid1 = int(o1["id"])
    co1 = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid1}/checkout-session",
        headers={"Authorization": f"Bearer {buyer1}"},
        timeout=5.0,
    )
    assert co1.status_code == 200

    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE orders SET expires_at = %s WHERE id = %s",
            (
                datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30),
                oid1,
            ),
        )
        conn.commit()

    rec_commerce = compose_cmd(
        "exec",
        "-T",
        "commerce-api-e2e",
        "python",
        "scripts/reconcile_orders.py",
        capture_output=True,
    )
    assert "expired=1" in rec_commerce.stdout
    assert get_order_detail(buyer1, oid1)["status"] == "expired"
    assert get_product_stock(pid) == 10

    # B) Пропуснат webhook в Stripe -> Payments reconcile_payments.py открива payment_status='paid' -> Commerce=paid
    email2 = f"k5_rec2_{uuid.uuid4().hex[:8]}@example.com"
    phone2 = f"+359883{uuid.uuid4().int % 1000000:06d}"
    _, buyer2 = register_and_login(email2)
    add_to_cart(buyer2, pid, 2)  # stock=8
    o2 = create_order(buyer2, phone=phone2)
    oid2 = int(o2["id"])
    co2 = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid2}/checkout-session",
        headers={"Authorization": f"Bearer {buyer2}"},
        timeout=5.0,
    )
    assert co2.status_code == 200
    s2_id = co2.json()["session_id"]

    httpx.post(
        f"{STRIPE_MOCK_URL}/_control/set_session",
        json={"session_id": s2_id, "status": "complete", "payment_status": "paid"},
        timeout=5.0,
    ).raise_for_status()

    rec_pay = compose_cmd(
        "exec",
        "-T",
        "payments-e2e",
        "python",
        "scripts/reconcile_payments.py",
        capture_output=True,
    )
    assert "paid=1" in rec_pay.stdout

    wait_until(
        "Order paid via reconcile_payments -> Outbox -> RabbitMQ -> Commerce",
        lambda: True if get_order_detail(buyer2, oid2)["status"] == "paid" else None,
    )
    assert get_product_stock(pid) == 8
    wait_until(
        "Reconcile delivered 1 email",
        lambda: m if len(m := get_mailpit_messages(email2)) == 1 else None,
    )
    wait_until(
        "Reconcile delivered 1 SMS",
        lambda: s if len(s := get_sms_sent_for_phone(phone2)) == 1 else None,
    )

    # C) Изгубен отговор при Stripe create -> initiated без stripe_session_id + изтекъл резерв
    # Нито reconcile_orders.py, нито reconcile_payments.py не освобождават stock, докато изходът е неизвестен
    email3 = f"k5_lost_exp_{uuid.uuid4().hex[:8]}@example.com"
    phone3 = f"+359881{uuid.uuid4().int % 1000000:06d}"
    _, buyer3 = register_and_login(email3)
    add_to_cart(buyer3, pid, 3)  # stock 8 -> 5
    o3 = create_order(buyer3, phone=phone3)
    oid3 = int(o3["id"])

    httpx.post(
        f"{STRIPE_MOCK_URL}/_control",
        json={"drop_next_create_after_commit": True},
        timeout=5.0,
    ).raise_for_status()
    r_drop = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid3}/checkout-session",
        headers={"Authorization": f"Bearer {buyer3}"},
        timeout=10.0,
    )
    assert r_drop.status_code == 502

    expired_ts = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE orders SET expires_at = %s WHERE id = %s", (expired_ts, oid3)
        )
        conn.commit()
    with psycopg.connect(PAYMENTS_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE payment_attempts SET order_expires_at = %s WHERE order_id = %s",
            (expired_ts, oid3),
        )
        conn.commit()

    # Докато Payments не изясни initiated без stripe_session_id, Commerce пази резерва (pending_payment)
    compose_cmd(
        "exec",
        "-T",
        "commerce-api-e2e",
        "python",
        "scripts/reconcile_orders.py",
        capture_output=True,
    )
    assert get_order_detail(buyer3, oid3)["status"] == "pending_payment"
    assert get_product_stock(pid) == 5

    # Payments reconcile в безопасното окно възстановява lost_sid със същия ключ, вижда open + изтекъл резерв -> expire в Stripe -> payment.expired
    sessions_snap = httpx.get(f"{STRIPE_MOCK_URL}/_sessions", timeout=5.0).json()[
        "sessions"
    ]
    lost_sessions = [
        s for s in sessions_snap if s.get("client_reference_id") == str(oid3)
    ]
    assert len(lost_sessions) == 1
    lost_sid = lost_sessions[0]["id"]

    rec_pay_lost = compose_cmd(
        "exec",
        "-T",
        "payments-e2e",
        "python",
        "scripts/reconcile_payments.py",
        capture_output=True,
    )
    assert "expired=1" in rec_pay_lost.stdout

    mock_lost = httpx.get(
        f"{STRIPE_MOCK_URL}/v1/checkout/sessions/{lost_sid}", timeout=5.0
    ).json()
    assert mock_lost["status"] == "expired"

    wait_until(
        "Recovered lost open expired session turned Commerce order expired and restored stock once",
        lambda: True if get_order_detail(buyer3, oid3)["status"] == "expired" else None,
    )
    assert get_product_stock(pid) == 8

    # Повторните сверки не връщат остатъка втори път
    compose_cmd(
        "exec",
        "-T",
        "commerce-api-e2e",
        "python",
        "scripts/reconcile_orders.py",
        capture_output=True,
    )
    compose_cmd(
        "exec",
        "-T",
        "payments-e2e",
        "python",
        "scripts/reconcile_payments.py",
        capture_output=True,
    )
    assert get_product_stock(pid) == 8
    assert get_mailpit_messages(email3) == []
    assert get_sms_sent_for_phone(phone3) == []

    # D) Изгубен отговор за сесия, която клиентът вече е платил -> reconcile_payments в безопасното окно я открива като paid
    email4 = f"k5_lost_paid_{uuid.uuid4().hex[:8]}@example.com"
    phone4 = f"+359880{uuid.uuid4().int % 1000000:06d}"
    _, buyer4 = register_and_login(email4)
    add_to_cart(buyer4, pid, 1)  # stock 8 -> 7
    o4 = create_order(buyer4, phone=phone4)
    oid4 = int(o4["id"])

    httpx.post(
        f"{STRIPE_MOCK_URL}/_control",
        json={"drop_next_create_after_commit": True},
        timeout=5.0,
    ).raise_for_status()
    r_drop4 = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid4}/checkout-session",
        headers={"Authorization": f"Bearer {buyer4}"},
        timeout=10.0,
    )
    assert r_drop4.status_code == 502

    sessions_snap4 = httpx.get(f"{STRIPE_MOCK_URL}/_sessions", timeout=5.0).json()[
        "sessions"
    ]
    lost_sid4 = next(
        s["id"] for s in sessions_snap4 if s.get("client_reference_id") == str(oid4)
    )
    httpx.post(
        f"{STRIPE_MOCK_URL}/_control/set_session",
        json={"session_id": lost_sid4, "status": "complete", "payment_status": "paid"},
        timeout=5.0,
    ).raise_for_status()

    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE orders SET expires_at = %s WHERE id = %s", (expired_ts, oid4)
        )
        conn.commit()
    with psycopg.connect(PAYMENTS_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE payment_attempts SET order_expires_at = %s WHERE order_id = %s",
            (expired_ts, oid4),
        )
        conn.commit()

    rec_pay4 = compose_cmd(
        "exec",
        "-T",
        "payments-e2e",
        "python",
        "scripts/reconcile_payments.py",
        capture_output=True,
    )
    assert "paid=1" in rec_pay4.stdout

    wait_until(
        "Lost initiated session paid in Stripe reaches paid via reconcile_payments",
        lambda: True if get_order_detail(buyer4, oid4)["status"] == "paid" else None,
    )
    assert get_product_stock(pid) == 7
    wait_until(
        "Lost paid recovery delivered 1 email",
        lambda: m if len(m := get_mailpit_messages(email4)) == 1 else None,
    )
    wait_until(
        "Lost paid recovery delivered 1 SMS",
        lambda: s if len(s := get_sms_sent_for_phone(phone4)) == 1 else None,
    )


def verify_payment_dlq_and_consumer_resilience(seller_token: str) -> None:
    print(
        "-> [K5-8] Poison / 5000-digit payment event goes to DLQ without leaking secrets or crashing consumer"
    )
    state_before = _get_container_state("commerce-payment-consumer-e2e")
    secret_marker = f"k5_dlq_marker_{uuid.uuid4().hex[:8]}"
    huge_digits = "9" * (getattr(sys, "get_int_max_str_digits", lambda: 4300)() + 700)

    conn = pika.BlockingConnection(
        pika.ConnectionParameters(
            host="127.0.0.1",
            port=55672,
            credentials=pika.PlainCredentials("guest", "guest"),
        )
    )
    try:
        ch = conn.channel()
        ch.confirm_delivery()
        poison_obj = json.dumps(
            {
                "event_id": f"bad-{secret_marker}",
                "event_type": f"payment.succeeded.{secret_marker}",
                "event_version": {"contact": secret_marker},
                "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
                "producer": "payments",
                "data": {"order_id": 1},
            }
        ).encode("utf-8")
        poison_5k = (
            f'{{"event_id": "{uuid.uuid4()}", "event_type": "payment.succeeded", '
            f'"event_version": {huge_digits}, "occurred_at": "{datetime.datetime.now(datetime.UTC).isoformat()}", '
            f'"producer": "payments", "data": {{"secret": "{secret_marker}"}}}}'
        ).encode()
        for body in (poison_obj, poison_5k):
            ch.basic_publish(
                exchange="payments.events",
                routing_key="payment.succeeded",
                body=body,
                properties=pika.BasicProperties(delivery_mode=2),
                mandatory=True,
            )
    finally:
        conn.close()

    wait_until(
        "Poison payment events moved to commerce.payment_events.dlq",
        lambda: (
            True
            if get_rabbitmq_queue_count("commerce.payment_events.dlq") >= 2
            else None
        ),
    )

    logs = compose_cmd(
        "logs", "--no-color", "commerce-payment-consumer-e2e", capture_output=True
    ).stdout
    assert secret_marker not in logs
    assert "9" * 64 not in logs

    state_after = _get_container_state("commerce-payment-consumer-e2e")
    assert state_after["id"] == state_before["id"]
    assert state_after["pid"] == state_before["pid"]
    assert state_after["restart_count"] == state_before["restart_count"] == 0

    # Потвърждаваме, че същият consumer обработва следващо нормално плащане
    email = f"k5_after_dlq_{uuid.uuid4().hex[:8]}@example.com"
    _, buyer = register_and_login(email)
    pid = create_product(seller_token, title="After DLQ", price_minor=1100, stock=5)
    add_to_cart(buyer, pid, 1)
    order = create_order(buyer)
    oid = int(order["id"])
    co = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid}/checkout-session",
        headers={"Authorization": f"Bearer {buyer}"},
        timeout=5.0,
    )
    assert co.status_code == 200
    r_wh = sign_and_post_stripe_webhook(
        {
            "id": f"evt_after_dlq_{uuid.uuid4().hex[:12]}",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": co.json()["session_id"],
                    "livemode": False,
                    "payment_status": "paid",
                    "amount_total": 1100,
                    "currency": "eur",
                    "client_reference_id": str(oid),
                    "metadata": {
                        "order_id": str(oid),
                        "attempt_key": f"order_{oid}_attempt_1",
                    },
                }
            },
        }
    )
    assert r_wh.status_code == 200
    wait_until(
        "Consumer still processes valid payment after DLQ poison batch",
        lambda: True if get_order_detail(buyer, oid)["status"] == "paid" else None,
    )


def verify_repeatable_cutover_migration(seller_token: str) -> None:
    print(
        "-> [K5-9] Repeatable cutover migration migrate_from_commerce.py + legacy open session webhook"
    )
    email = f"k5_cutover_{uuid.uuid4().hex[:8]}@example.com"
    phone = f"+359882{uuid.uuid4().int % 1000000:06d}"
    _, buyer = register_and_login(email)
    pid = create_product(
        seller_token, title="Legacy Session Prod", price_minor=4500, stock=5
    )
    add_to_cart(buyer, pid, 1)
    order = create_order(buyer, phone=phone)
    oid = int(order["id"])

    legacy_sess_id = f"cs_legacy_cutover_{uuid.uuid4().hex[:10]}"
    legacy_attempt_key = f"order_{oid}_attempt_1"
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stripe_checkout_sessions
                (order_id, status, amount_minor, currency, mode, stripe_session_id, checkout_url, idempotency_key)
            VALUES (%s, 'open', 4500, 'EUR', 'test', %s, %s, %s)
            RETURNING id
            """,
            (
                oid,
                legacy_sess_id,
                f"https://checkout.stripe.mock/pay/{legacy_sess_id}",
                legacy_attempt_key,
            ),
        )
        legacy_row_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO stripe_processed_events (stripe_event_id, event_type, order_id)
            VALUES (%s, 'checkout.session.created', %s)
            """,
            (f"evt_legacy_seen_{uuid.uuid4().hex[:10]}", oid),
        )
        conn.commit()

    # Стартираме миграцията ДВА пъти подред за проверка на идемпотентността
    for run_no in (1, 2):
        mig = compose_cmd(
            "exec",
            "-T",
            "payments-e2e",
            "python",
            "scripts/migrate_from_commerce.py",
            "--commerce-url",
            ADMIN_COMMERCE_DSN,
            "--payments-url",
            CONTAINER_PAYMENTS_DSN,
            capture_output=True,
        )
        assert mig.returncode == 0, f"Migration run {run_no} failed: {mig.stderr}"

    # Старите таблици в Commerce остават непокътнати
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM stripe_checkout_sessions WHERE id = %s",
            (legacy_row_id,),
        )
        assert cur.fetchone()[0] == 1

    # Webhook за мигрираната стара отворена сесия минава през Payments и плаща поръчката в Commerce
    r_wh = sign_and_post_stripe_webhook(
        {
            "id": f"evt_legacy_wh_{uuid.uuid4().hex[:10]}",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": legacy_sess_id,
                    "livemode": False,
                    "payment_status": "paid",
                    "amount_total": 4500,
                    "currency": "eur",
                    "client_reference_id": str(oid),
                    "metadata": {
                        "order_id": str(oid),
                        "attempt_id": str(legacy_row_id),
                    },
                }
            },
        }
    )
    assert r_wh.status_code == 200
    assert r_wh.json()["status"] == "success"

    wait_until(
        "Legacy migrated session turned Commerce order paid",
        lambda: True if get_order_detail(buyer, oid)["status"] == "paid" else None,
    )
    wait_until(
        "Legacy cutover delivered 1 email",
        lambda: m if len(m := get_mailpit_messages(email)) == 1 else None,
    )
    wait_until(
        "Legacy cutover delivered 1 SMS",
        lambda: s if len(s := get_sms_sent_for_phone(phone)) == 1 else None,
    )

    # Допълнителна проверка #6: мигрирана pending_payment поръчка с терминална (expired) сесия получава нов attempt_2 при checkout
    email_term = f"k5_cutover_term_{uuid.uuid4().hex[:8]}@example.com"
    _, buyer_term = register_and_login(email_term)
    add_to_cart(buyer_term, pid, 1)
    order_term = create_order(buyer_term)
    oid_term = int(order_term["id"])
    old_exp_sid = f"cs_legacy_exp_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stripe_checkout_sessions
                (order_id, status, amount_minor, currency, mode, stripe_session_id, checkout_url, idempotency_key)
            VALUES (%s, 'expired', 4500, 'EUR', 'test', %s, %s, %s)
            """,
            (
                oid_term,
                old_exp_sid,
                f"https://checkout.stripe.mock/pay/{old_exp_sid}",
                f"order_{oid_term}_attempt_1",
            ),
        )
        cur.execute(
            "UPDATE orders SET payment_attempt_seq = 2, active_attempt_key = %s WHERE id = %s",
            (f"order_{oid_term}_attempt_2", oid_term),
        )
        conn.commit()

    mig_term = compose_cmd(
        "exec",
        "-T",
        "payments-e2e",
        "python",
        "scripts/migrate_from_commerce.py",
        "--commerce-url",
        ADMIN_COMMERCE_DSN,
        "--payments-url",
        CONTAINER_PAYMENTS_DSN,
        capture_output=True,
    )
    assert mig_term.returncode == 0, f"Terminal migration failed: {mig_term.stderr}"

    co_fresh = httpx.post(
        f"{COMMERCE_API_URL}/orders/{oid_term}/checkout-session",
        headers={"Authorization": f"Bearer {buyer_term}"},
        timeout=10.0,
    )
    assert co_fresh.status_code == 200, (
        f"Expected fresh checkout session after expired cutover, got {co_fresh.text}"
    )
    assert co_fresh.json()["session_id"] != old_exp_sid


def main() -> None:
    wait_until(
        "Commerce API ready",
        lambda: (
            True
            if httpx.get(f"{COMMERCE_API_URL}/openapi.json", timeout=2.0).status_code
            == 200
            else None
        ),
    )
    wait_until(
        "Payments API ready",
        lambda: (
            True
            if httpx.get(f"{PAYMENTS_API_URL}/health", timeout=2.0).status_code == 200
            else None
        ),
    )
    wait_until(
        "Mailpit ready",
        lambda: (
            True
            if httpx.get(f"{MAILPIT_API_URL}/api/v1/messages", timeout=2.0).status_code
            == 200
            else None
        ),
    )
    wait_until(
        "SMS mock ready",
        lambda: (
            True
            if httpx.get(f"{SMS_MOCK_URL}/health", timeout=2.0).status_code == 200
            else None
        ),
    )
    wait_until(
        "Stripe mock ready",
        lambda: (
            True
            if httpx.get(f"{STRIPE_MOCK_URL}/health", timeout=2.0).status_code == 200
            else None
        ),
    )

    verify_boundaries_and_isolation()
    _, seller_token = register_and_login(
        f"k5_seller_{uuid.uuid4().hex[:8]}@example.com"
    )

    verify_concurrent_and_lost_stripe_response(seller_token)
    verify_commerce_offline_during_webhook_then_recovery(seller_token)
    verify_rabbitmq_down_during_payment_recovery(seller_token)
    verify_async_failure_retry_and_success(seller_token)
    verify_cancel_and_late_payment_discrepancy(seller_token)
    verify_reconciliation_scripts(seller_token)
    verify_payment_dlq_and_consumer_resilience(seller_token)
    verify_repeatable_cutover_migration(seller_token)
    print("✅ All K5 isolated E2E scenarios passed!")


if __name__ == "__main__":
    main()
