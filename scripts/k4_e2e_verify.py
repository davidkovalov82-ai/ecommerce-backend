"""Изолирана E2E проверка на К4 върху реални PostgreSQL, RabbitMQ и Mailpit контейнери."""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pika
import psycopg
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

# Задаваме E2E настройки преди импорт на app модулите от services/notifications
ROOT_DIR = Path(__file__).resolve().parent.parent
NOTIFICATIONS_DIR = ROOT_DIR / "services" / "notifications"
if str(NOTIFICATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(NOTIFICATIONS_DIR))

os.environ["DB_HOST"] = "127.0.0.1"
os.environ["DB_PORT"] = "55432"
os.environ["DB_USER"] = "notifications"
os.environ["DB_PASSWORD"] = "notifications"
os.environ["DB_NAME"] = "notifications"
os.environ["RABBITMQ_HOST"] = "127.0.0.1"
os.environ["RABBITMQ_PORT"] = "55672"
os.environ["RABBITMQ_USER"] = "guest"
os.environ["RABBITMQ_PASSWORD"] = "guest"

from app.models import NotificationDelivery

COMPOSE_PROJECT = "commerce-k4-e2e"
COMPOSE_FILE = str(ROOT_DIR / "compose.e2e.yaml")

COMMERCE_API_URL = "http://127.0.0.1:58000"
MAILPIT_API_URL = "http://127.0.0.1:58025"
SMS_MOCK_URL = "http://127.0.0.1:58080"
STRIPE_WEBHOOK_SECRET = "whsec_k4_e2e_secret_stripe_commerce_32_bytes_ok"

COMMERCE_DSN = "postgresql://commerce:commerce@127.0.0.1:55432/commerce"
NOTIFICATIONS_DSN = (
    "postgresql://notifications:notifications@127.0.0.1:55432/notifications"
)
NOTIFICATIONS_SQLALCHEMY_URL = (
    "postgresql+psycopg://notifications:notifications@127.0.0.1:55432/notifications"
)

DEFAULT_ADDRESS = {
    "recipient_name": "Иван Петров",
    "phone": "+359888100200",
    "country_code": "BG",
    "city": "Sofia",
    "postal_code": "1000",
    "address_line1": "ul. Graf Ignatiev 10",
}


def wait_until[T](
    description: str,
    predicate: Callable[[], T | None],
    *,
    timeout_seconds: float = 35.0,
    poll_interval: float = 0.25,
) -> T:
    """Изчаква условие с краен таймаут без безкрайни цикли."""
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


def compose_cmd(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", COMPOSE_FILE, *args],
        cwd=ROOT_DIR,
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
    assert reg.status_code == 201, f"User register failed: {reg.text}"
    user_id = reg.json()["id"]

    login = httpx.post(
        f"{COMMERCE_API_URL}/auth/login",
        json={"email": email, "password": password},
        timeout=5.0,
    )
    assert login.status_code == 200, f"Login failed: {login.text}"
    return user_id, login.json()["access_token"]


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
    assert resp.status_code == 201, f"Create product failed: {resp.text}"
    return int(resp.json()["id"])


def add_to_cart(token: str, product_id: int, quantity: int) -> None:
    resp = httpx.put(
        f"{COMMERCE_API_URL}/cart/items/{product_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"quantity": quantity},
        timeout=5.0,
    )
    assert resp.status_code == 200, f"Add to cart failed: {resp.text}"


def create_order_and_pay_via_stripe_webhook(
    token: str,
    *,
    phone: str = "+359888100200",
) -> tuple[int, int]:
    """Създава поръчка през API и я потвърждава единствено през Stripe webhook пътя."""
    address = {**DEFAULT_ADDRESS, "phone": phone}
    order_resp = httpx.post(
        f"{COMMERCE_API_URL}/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
        json={"shipping_address": address},
        timeout=5.0,
    )
    assert order_resp.status_code == 201, f"Order create failed: {order_resp.text}"
    order_data = order_resp.json()
    order_id = int(order_data["id"])
    total_minor = int(order_data["total_minor"])

    # Създаваме отворен StripeCheckoutSession запис (без директно писане на paid в orders!)
    stripe_session_id = f"cs_e2e_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(COMMERCE_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO stripe_checkout_sessions
                    (order_id, status, amount_minor, currency, mode, stripe_session_id, checkout_url, idempotency_key)
                VALUES (%s, 'open', %s, 'EUR', 'test', %s, %s, %s)
                RETURNING id
                """,
                (
                    order_id,
                    total_minor,
                    stripe_session_id,
                    f"https://checkout.stripe.com/pay/{stripe_session_id}",
                    f"idemp_{stripe_session_id}",
                ),
            )
            attempt_id = cur.fetchone()[0]
        conn.commit()

    webhook_event = {
        "id": f"evt_e2e_{uuid.uuid4().hex[:16]}",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": stripe_session_id,
                "livemode": False,
                "payment_status": "paid",
                "amount_total": total_minor,
                "currency": "eur",
                "client_reference_id": str(order_id),
                "metadata": {
                    "order_id": str(order_id),
                    "attempt_id": str(attempt_id),
                },
            }
        },
    }
    payload_bytes = json.dumps(webhook_event).encode("utf-8")
    ts = int(time.time())
    signed = f"{ts}.".encode() + payload_bytes
    sig = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode("utf-8"),
        signed,
        hashlib.sha256,
    ).hexdigest()

    wh_resp = httpx.post(
        f"{COMMERCE_API_URL}/webhooks/stripe",
        content=payload_bytes,
        headers={"Stripe-Signature": f"t={ts},v1={sig}"},
        timeout=5.0,
    )
    assert wh_resp.status_code == 200, f"Webhook failed: {wh_resp.text}"
    assert wh_resp.json()["status"] == "success"

    check_order = httpx.get(
        f"{COMMERCE_API_URL}/orders/{order_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5.0,
    )
    assert check_order.status_code == 200
    assert check_order.json()["status"] == "paid"
    return order_id, total_minor


def get_mailpit_messages(recipient_email: str) -> list[dict[str, Any]]:
    resp = httpx.get(f"{MAILPIT_API_URL}/api/v1/messages", timeout=3.0)
    resp.raise_for_status()
    messages = resp.json().get("messages", [])
    return [
        m
        for m in messages
        if any(to.get("Address") == recipient_email for to in m.get("To", []))
    ]


def get_mailpit_detail(msg_id: str) -> dict[str, Any]:
    resp = httpx.get(f"{MAILPIT_API_URL}/api/v1/message/{msg_id}", timeout=3.0)
    resp.raise_for_status()
    return resp.json()


def get_sms_sent_for_phone(phone: str) -> list[dict[str, Any]]:
    resp = httpx.get(f"{SMS_MOCK_URL}/_sent", timeout=3.0)
    resp.raise_for_status()
    return [m for m in resp.json().get("messages", []) if m.get("to") == phone]


def get_outbox_rows(order_id: int) -> list[dict[str, Any]]:
    with psycopg.connect(COMMERCE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, event_id, event_type, status, retry_count, last_error, shipment_id
            FROM order_outbox_events
            WHERE order_id = %s
            ORDER BY id ASC
            """,
            (order_id,),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def get_rabbitmq_queue_count(queue_name: str) -> int:
    params = pika.ConnectionParameters(
        host="127.0.0.1",
        port=55672,
        credentials=pika.PlainCredentials("guest", "guest"),
        connection_attempts=1,
        socket_timeout=3.0,
    )
    conn = pika.BlockingConnection(params)
    try:
        ch = conn.channel()
        res = ch.queue_declare(queue=queue_name, passive=True)
        return int(res.method.message_count)
    finally:
        conn.close()


def verify_db_isolation() -> None:
    print(
        "-> [Isolation] Checking role 'notifications' cannot connect to 'commerce' DB..."
    )
    bad_dsn = "postgresql://notifications:notifications@127.0.0.1:55432/commerce"
    try:
        with psycopg.connect(bad_dsn, connect_timeout=3):
            raise AssertionError(
                "notifications role MUST NOT access commerce database!"
            )
    except psycopg.OperationalError:
        pass


def verify_scenario_1(seller_token: str) -> None:
    print(
        "-> [Scenario 1] Notifications stopped -> payment succeeds -> starts -> email & SMS delivered once"
    )
    compose_cmd("stop", "notifications-e2e")

    email = f"s1_{uuid.uuid4().hex[:8]}@example.com"
    phone = f"+359881{uuid.uuid4().int % 1000000:06d}"
    _, buyer_token = register_and_login(email)
    pid = create_product(seller_token, title="E2E Item S1", price_minor=3500)
    add_to_cart(buyer_token, pid, 1)

    order_id, _ = create_order_and_pay_via_stripe_webhook(buyer_token, phone=phone)

    # Commerce публикува в RabbitMQ докато Notifications е спрян
    wait_until(
        "Scenario 1 outbox event published",
        lambda: (
            (rows := get_outbox_rows(order_id))
            and rows[0]["status"] == "published"
            and rows[0]
        ),
    )
    assert len(get_mailpit_messages(email)) == 0
    assert len(get_sms_sent_for_phone(phone)) == 0

    # Пускаме Notifications без второ плащане или webhook
    compose_cmd("start", "notifications-e2e")

    msgs = wait_until(
        "Scenario 1 Mailpit email delivered",
        lambda: m if len(m := get_mailpit_messages(email)) == 1 else None,
    )
    detail = get_mailpit_detail(msgs[0]["ID"])
    assert f"#{order_id}" in detail["Subject"]
    assert "35.00" in detail["Text"]

    sms_list = wait_until(
        "Scenario 1 SMS delivered",
        lambda: s if len(s := get_sms_sent_for_phone(phone)) == 1 else None,
    )
    assert f"#{order_id}" in sms_list[0]["body"]


def verify_scenario_2(seller_token: str) -> None:
    print(
        "-> [Scenario 2] RabbitMQ stopped -> order becomes paid, stays in Outbox -> recovers -> notification arrives"
    )
    compose_cmd("stop", "rabbitmq-e2e")

    email = f"s2_{uuid.uuid4().hex[:8]}@example.com"
    phone = f"+359882{uuid.uuid4().int % 1000000:06d}"
    _, buyer_token = register_and_login(email)
    pid = create_product(seller_token, title="E2E Item S2", price_minor=4200)
    add_to_cart(buyer_token, pid, 1)

    order_id, _ = create_order_and_pay_via_stripe_webhook(buyer_token, phone=phone)

    # Изчакваме outbox daemon-ът да направи поне 1 неуспешен опит при спрян брокер
    row_pending = wait_until(
        "Scenario 2 outbox retry_count >= 1 while RabbitMQ down",
        lambda: (
            r[0]
            if (r := get_outbox_rows(order_id))
            and r[0]["status"] == "pending"
            and r[0]["retry_count"] >= 1
            else None
        ),
    )
    stable_event_id = row_pending["event_id"]
    assert len(get_mailpit_messages(email)) == 0

    # Възстановяваме RabbitMQ
    compose_cmd("start", "rabbitmq-e2e")
    wait_until(
        "Scenario 2 RabbitMQ healthy and outbox published with same event_id",
        lambda: (
            r[0]
            if (r := get_outbox_rows(order_id))
            and r[0]["status"] == "published"
            and r[0]["event_id"] == stable_event_id
            else None
        ),
    )

    wait_until(
        "Scenario 2 Mailpit email delivered after broker recovery",
        lambda: m if len(m := get_mailpit_messages(email)) == 1 else None,
    )
    wait_until(
        "Scenario 2 SMS delivered after broker recovery",
        lambda: s if len(s := get_sms_sent_for_phone(phone)) == 1 else None,
    )


def verify_scenario_3(seller_token: str) -> None:
    print(
        "-> [Scenario 3] Notifications commits DB then drops connection BEFORE ack -> real RabbitMQ redelivers -> 1 task per channel"
    )
    # Спираме контейнера notifications-e2e, за да изпълним реален consumer с прекъсване на TCP преди ack
    compose_cmd("stop", "notifications-e2e")
    try:
        # Уверяваме се, че опашката е празна преди сценария
        wait_until(
            "notifications.events queue empty before Scenario 3",
            lambda: (
                True if get_rabbitmq_queue_count("notifications.events") == 0 else None
            ),
        )

        email = f"s3_{uuid.uuid4().hex[:8]}@example.com"
        phone = f"+359883{uuid.uuid4().int % 1000000:06d}"
        _, buyer_token = register_and_login(email)
        pid = create_product(seller_token, title="E2E Item S3", price_minor=1900)
        add_to_cart(buyer_token, pid, 1)

        order_id, _ = create_order_and_pay_via_stripe_webhook(buyer_token, phone=phone)

        row = wait_until(
            "Scenario 3 outbox published to RabbitMQ",
            lambda: (
                r[0]
                if (r := get_outbox_rows(order_id)) and r[0]["status"] == "published"
                else None
            ),
        )
        target_event_id = row["event_id"]

        wait_until(
            "Scenario 3 message sitting in real RabbitMQ queue",
            lambda: (
                True if get_rabbitmq_queue_count("notifications.events") >= 1 else None
            ),
        )

        # Стартираме реален consumer в отделен процес, който прекратява процеса/връзката (os._exit)
        # веднага след DB commit и ПРЕДИ RabbitMQ basic_ack
        crash_script = """
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.consumer import RabbitMQEventConsumer
from app.models import InboxEvent, NotificationDelivery

target_event_id = os.environ["TARGET_EVENT_ID"]
db_url = os.environ["NOTIFICATIONS_SQLALCHEMY_URL"]
engine = create_engine(db_url, pool_pre_ping=True)
sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

def drop_before_ack(ev, _ch, _method):
    if ev.event_id == target_event_id:
        with sf() as s:
            assert s.get(InboxEvent, target_event_id) is not None
            deliveries = s.query(NotificationDelivery).filter(NotificationDelivery.event_id == target_event_id).count()
            assert deliveries == 2
        os._exit(137)

consumer = RabbitMQEventConsumer(
    session_factory=sf,
    host="127.0.0.1",
    port=55672,
    before_ack_hook=drop_before_ack,
)
consumer.consume_available(max_messages=10, idle_timeout_seconds=1.0)
"""
        env = dict(os.environ)
        env["TARGET_EVENT_ID"] = target_event_id
        env["NOTIFICATIONS_SQLALCHEMY_URL"] = NOTIFICATIONS_SQLALCHEMY_URL
        proc = subprocess.run(
            [sys.executable, "-c", crash_script],
            cwd=NOTIFICATIONS_DIR,
            env=env,
            check=False,
        )
        assert proc.returncode == 137, (
            f"Expected crash return code 137 before ack, got {proc.returncode}"
        )

        # Потвърждаваме, че истинският RabbitMQ е върнал непотвърденото съобщение обратно в опашката
        wait_until(
            "Scenario 3 unacked message requeued by real RabbitMQ",
            lambda: (
                True if get_rabbitmq_queue_count("notifications.events") >= 1 else None
            ),
        )
    finally:
        compose_cmd("start", "notifications-e2e")

    # След рестарта notifications-e2e приема redelivery от RabbitMQ и доставя точно веднъж
    wait_until(
        "Scenario 3 Mailpit email delivered once",
        lambda: m if len(m := get_mailpit_messages(email)) == 1 else None,
    )
    wait_until(
        "Scenario 3 SMS delivered once",
        lambda: s if len(s := get_sms_sent_for_phone(phone)) == 1 else None,
    )

    engine = create_engine(NOTIFICATIONS_SQLALCHEMY_URL, pool_pre_ping=True)
    notif_sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:

        def _both_sent() -> list[NotificationDelivery] | None:
            with notif_sf() as s:
                rows = (
                    s.execute(
                        select(NotificationDelivery)
                        .where(NotificationDelivery.event_id == target_event_id)
                        .order_by(NotificationDelivery.channel)
                    )
                    .scalars()
                    .all()
                )
                if len(rows) == 2 and all(d.status == "sent" for d in rows):
                    return rows
                return None

        deliveries = wait_until("Scenario 3 exactly 2 sent delivery rows", _both_sent)
        assert [d.channel for d in deliveries] == ["email", "sms"]
        assert len(get_mailpit_messages(email)) == 1
        assert len(get_sms_sent_for_phone(phone)) == 1
    finally:
        engine.dispose()


def verify_scenario_4(seller_token: str) -> None:
    print(
        "-> [Scenario 4] SMTP down -> SMS succeeds independently -> SMTP recovers -> only email retries"
    )
    compose_cmd("stop", "mailpit-e2e")

    email = f"s4_{uuid.uuid4().hex[:8]}@example.com"
    phone = f"+359884{uuid.uuid4().int % 1000000:06d}"
    _, buyer_token = register_and_login(email)
    pid = create_product(seller_token, title="E2E Item S4", price_minor=5100)
    add_to_cart(buyer_token, pid, 1)

    order_id, _ = create_order_and_pay_via_stripe_webhook(buyer_token, phone=phone)
    row = wait_until(
        "Scenario 4 outbox published",
        lambda: (
            r[0]
            if (r := get_outbox_rows(order_id)) and r[0]["status"] == "published"
            else None
        ),
    )
    event_id = row["event_id"]

    engine = create_engine(NOTIFICATIONS_SQLALCHEMY_URL, pool_pre_ping=True)
    notif_sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:

        def _sms_done_email_pending() -> bool:
            with notif_sf() as s:
                rows = {
                    d.channel: d
                    for d in s.execute(
                        select(NotificationDelivery).where(
                            NotificationDelivery.event_id == event_id
                        )
                    )
                    .scalars()
                    .all()
                }
                return (
                    "sms" in rows
                    and "email" in rows
                    and rows["sms"].status == "sent"
                    and rows["sms"].attempt_count == 1
                    and rows["email"].status == "pending"
                    and rows["email"].attempt_count >= 1
                    and bool(rows["email"].last_error)
                )

        wait_until(
            "Scenario 4 SMS sent independently while email fails SMTP",
            _sms_done_email_pending,
        )
        assert len(get_sms_sent_for_phone(phone)) == 1

        # Възстановяваме SMTP (Mailpit)
        compose_cmd("start", "mailpit-e2e")

        def _email_recovered_sms_not_repeated() -> bool:
            with notif_sf() as s:
                rows = {
                    d.channel: d
                    for d in s.execute(
                        select(NotificationDelivery).where(
                            NotificationDelivery.event_id == event_id
                        )
                    )
                    .scalars()
                    .all()
                }
                return (
                    rows["email"].status == "sent"
                    and rows["sms"].status == "sent"
                    and rows["sms"].attempt_count == 1
                )

        wait_until(
            "Scenario 4 email retried and delivered after SMTP recovery",
            _email_recovered_sms_not_repeated,
        )
        wait_until(
            "Scenario 4 Mailpit received email",
            lambda: m if len(m := get_mailpit_messages(email)) == 1 else None,
        )
        # Потвърждаваме, че SMS mock е получил точно 1 SMS за цялото време
        assert len(get_sms_sent_for_phone(phone)) == 1
    finally:
        engine.dispose()


def verify_scenario_5() -> None:
    print(
        "-> [Scenario 5] Two sellers ship two shipments -> two separate events & two emails with isolated items"
    )
    _, anna_token = register_and_login(f"anna_{uuid.uuid4().hex[:6]}@example.com")
    _, boris_token = register_and_login(f"boris_{uuid.uuid4().hex[:6]}@example.com")
    buyer_email = f"s5_clara_{uuid.uuid4().hex[:8]}@example.com"
    buyer_phone = f"+359885{uuid.uuid4().int % 1000000:06d}"
    _, buyer_token = register_and_login(buyer_email)

    p_headphones = create_product(anna_token, title="Слушалки Sony", price_minor=5000)
    p_cable = create_product(anna_token, title="USB-C кабел", price_minor=1500)
    p_book = create_product(boris_token, title="Книга Python", price_minor=2000)

    add_to_cart(buyer_token, p_headphones, 1)
    add_to_cart(buyer_token, p_cable, 2)
    add_to_cart(buyer_token, p_book, 1)

    order_id, _ = create_order_and_pay_via_stripe_webhook(
        buyer_token, phone=buyer_phone
    )

    # Изчакваме първото писмо (за плащането)
    wait_until(
        "Scenario 5 order.paid email delivered",
        lambda: m if len(m := get_mailpit_messages(buyer_email)) == 1 else None,
    )

    # Намираме пратките на Анна и Борис през API
    anna_shipments = httpx.get(
        f"{COMMERCE_API_URL}/seller/shipments",
        headers={"Authorization": f"Bearer {anna_token}"},
        timeout=5.0,
    ).json()["items"]
    boris_shipments = httpx.get(
        f"{COMMERCE_API_URL}/seller/shipments",
        headers={"Authorization": f"Bearer {boris_token}"},
        timeout=5.0,
    ).json()["items"]

    anna_sh_id = next(s["id"] for s in anna_shipments if s["order_id"] == order_id)
    boris_sh_id = next(s["id"] for s in boris_shipments if s["order_id"] == order_id)

    ship1 = httpx.post(
        f"{COMMERCE_API_URL}/seller/shipments/{anna_sh_id}/ship",
        headers={"Authorization": f"Bearer {anna_token}"},
        timeout=5.0,
    )
    assert ship1.status_code == 200
    ship2 = httpx.post(
        f"{COMMERCE_API_URL}/seller/shipments/{boris_sh_id}/ship",
        headers={"Authorization": f"Bearer {boris_token}"},
        timeout=5.0,
    )
    assert ship2.status_code == 200

    # Очакваме общо 3 имейла: 1 order.paid + 2 shipment.shipped
    all_msgs = wait_until(
        "Scenario 5 all 3 emails delivered (paid + 2 shipments)",
        lambda: m if len(m := get_mailpit_messages(buyer_email)) == 3 else None,
    )

    details = [get_mailpit_detail(m["ID"]) for m in all_msgs]
    paid_email = next(
        d for d in details if f"платена поръчка #{order_id}" in d["Subject"]
    )
    assert "100.00 EUR" in paid_email["Text"]
    ship_anna = next(d for d in details if f"пратка #{anna_sh_id} " in d["Subject"])
    ship_boris = next(d for d in details if f"пратка #{boris_sh_id} " in d["Subject"])

    assert "Слушалки Sony" in ship_anna["Text"]
    assert "USB-C кабел" in ship_anna["Text"]
    assert "Книга Python" not in ship_anna["Text"]

    assert "Книга Python" in ship_boris["Text"]
    assert "Слушалки Sony" not in ship_boris["Text"]
    assert "USB-C кабел" not in ship_boris["Text"]

    wait_until(
        "Scenario 5 3 SMS delivered (paid + 2 shipments)",
        lambda: s if len(s := get_sms_sent_for_phone(buyer_phone)) == 3 else None,
    )

    outbox_rows = get_outbox_rows(order_id)
    assert len(outbox_rows) == 3
    assert len({r["event_id"] for r in outbox_rows}) == 3


def verify_dlq_and_legacy_no_phone() -> None:
    print(
        "-> [Bonus] Unknown event_version -> DLQ, and legacy event without phone -> Email sent + SMS skipped"
    )
    params = pika.ConnectionParameters(
        host="127.0.0.1",
        port=55672,
        credentials=pika.PlainCredentials("guest", "guest"),
        connection_attempts=1,
        socket_timeout=3.0,
    )
    conn = pika.BlockingConnection(params)
    legacy_event_id = str(uuid.uuid4())
    legacy_email = f"legacy_nophone_{uuid.uuid4().hex[:8]}@example.com"
    try:
        ch = conn.channel()
        ch.confirm_delivery()

        # Проверка за липсващ маршрут: mandatory=True + confirm_delivery -> UnroutableError
        try:
            ch.basic_publish(
                exchange="commerce.events",
                routing_key="order.unroutable_test",
                body=b"{}",
                properties=pika.BasicProperties(delivery_mode=2),
                mandatory=True,
            )
            raise AssertionError("Expected UnroutableError for unbound routing key")
        except pika.exceptions.UnroutableError:
            pass

        bad_version_msg = json.dumps(
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "order.paid",
                "event_version": 99,
                "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
                "producer": "commerce",
                "data": {
                    "order_id": 999,
                    "customer_email": "bad@example.com",
                    "total_minor": 100,
                    "currency": "EUR",
                },
            }
        ).encode("utf-8")
        ch.basic_publish(
            exchange="commerce.events",
            routing_key="order.paid",
            body=bad_version_msg,
            properties=pika.BasicProperties(
                delivery_mode=2, content_type="application/json"
            ),
            mandatory=True,
        )

        # Старо събитие без полето customer_phone
        legacy_msg = json.dumps(
            {
                "event_id": legacy_event_id,
                "event_type": "order.paid",
                "event_version": 1,
                "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
                "producer": "commerce",
                "data": {
                    "order_id": 999001,
                    "customer_email": legacy_email,
                    "total_minor": 2500,
                    "currency": "EUR",
                },
            }
        ).encode("utf-8")
        ch.basic_publish(
            exchange="commerce.events",
            routing_key="order.paid",
            body=legacy_msg,
            properties=pika.BasicProperties(
                delivery_mode=2, content_type="application/json"
            ),
            mandatory=True,
        )
    finally:
        conn.close()

    wait_until(
        "Unknown event_version dead-lettered into notifications.events.dlq",
        lambda: (
            True if get_rabbitmq_queue_count("notifications.events.dlq") >= 1 else None
        ),
    )
    wait_until(
        "Legacy event email delivered",
        lambda: m if len(m := get_mailpit_messages(legacy_email)) == 1 else None,
    )

    engine = create_engine(NOTIFICATIONS_SQLALCHEMY_URL, pool_pre_ping=True)
    notif_sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:

        def _legacy_statuses() -> dict[str, NotificationDelivery] | None:
            with notif_sf() as s:
                rows = {
                    d.channel: d
                    for d in s.execute(
                        select(NotificationDelivery).where(
                            NotificationDelivery.event_id == legacy_event_id
                        )
                    )
                    .scalars()
                    .all()
                }
                if (
                    "email" in rows
                    and "sms" in rows
                    and rows["email"].status == "sent"
                    and rows["sms"].status == "skipped"
                ):
                    return rows
                return None

        rows = wait_until(
            "Legacy event email sent & sms skipped with reason", _legacy_statuses
        )
        assert rows["sms"].last_error == "missing_customer_phone"
    finally:
        engine.dispose()


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

    verify_db_isolation()
    _, shared_seller_token = register_and_login(
        f"seller_{uuid.uuid4().hex[:8]}@example.com"
    )

    verify_scenario_1(shared_seller_token)
    verify_scenario_2(shared_seller_token)
    verify_scenario_3(shared_seller_token)
    verify_scenario_4(shared_seller_token)
    verify_scenario_5()
    verify_dlq_and_legacy_no_phone()
    print("✅ All K4 isolated E2E scenarios passed!")


if __name__ == "__main__":
    main()
