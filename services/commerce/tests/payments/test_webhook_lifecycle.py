import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.modules.accounts.repository import UserRepository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.models import Product
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.models import (
    Order,
    OrderInboxEvent,
    OrderOutboxEvent,
    OrderPaymentDiscrepancy,
)
from app.modules.orders.payment_consumer import RabbitMQPaymentEventConsumer
from app.modules.orders.payments_client import PaymentsClient
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from tests.payments.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
    make_payment_event,
    make_payment_event_envelope,
)

client = TestClient(app)


def _new_service(session: Session) -> OrderService:
    return OrderService(
        order_repository=OrderRepository(session),
        cart_repository=CartRepository(session),
        product_repository=ProductRepository(session),
        user_repository=UserRepository(session),
        session=session,
        payments_client=MagicMock(spec=PaymentsClient),
    )


def test_payment_succeeded_marks_paid_preserves_stock_and_writes_single_outbox(
    db_session: Session,
) -> None:
    """payment.succeeded превежда поръчката в paid, пази stock и записва 1 order.paid и inbox."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Widget", price_minor=1500, stock=10)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock=8
    assert order.total_minor == 3000

    svc = _new_service(db_session)
    ev = make_payment_event(
        event_type="payment.succeeded",
        order_id=order.id,
        amount_minor=3000,
    )
    assert svc.apply_payment_event(ev) == "paid"

    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == "paid"
    assert product.stock == 8

    outbox = (
        db_session.query(OrderOutboxEvent)
        .filter(OrderOutboxEvent.order_id == order.id)
        .all()
    )
    assert len(outbox) == 1
    assert outbox[0].event_type == "order.paid"

    inbox = db_session.get(OrderInboxEvent, ev.event_id)
    assert inbox is not None
    assert inbox.outcome == "paid"


def test_consumer_commits_before_ack_and_redelivery_is_idempotent(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Ако consumer падне след commit преди basic_ack, повторната доставка не дублира order.paid."""
    user, _ = _create_user_and_token(db_session, email="ack_loss@example.com")
    product = _create_product(db_session, "AckLossProd", price_minor=2000, stock=10)
    order = _create_order(db_session, user.id, product, quantity=3)  # stock=7

    envelope = make_payment_event_envelope(
        event_type="payment.succeeded",
        order_id=order.id,
        amount_minor=6000,
    )
    raw_body = json.dumps(envelope).encode("utf-8")

    # Първа доставка: симулираме срив точно преди basic_ack
    def fail_before_ack(_ev, _ch, _method):  # type: ignore[no-untyped-def]
        raise RuntimeError("Simulated crash before basic_ack")

    consumer1 = RabbitMQPaymentEventConsumer(
        session_factory=session_factory,
        before_ack_hook=fail_before_ack,
    )
    ch1 = MagicMock()
    with pytest.raises(RuntimeError, match="Simulated crash before basic_ack"):
        consumer1._handle_message(
            ch1, SimpleNamespace(delivery_tag=101), None, raw_body
        )
    ch1.basic_ack.assert_not_called()

    # Транзакцията вече е commit-ната в БД
    with session_factory() as s:
        assert s.get(Order, order.id).status == "paid"  # type: ignore[union-attr]
        assert s.get(Product, product.id).stock == 7  # type: ignore[union-attr]
        assert (
            s.query(OrderOutboxEvent)
            .filter(OrderOutboxEvent.order_id == order.id)
            .count()
            == 1
        )

    # Повторна доставка от RabbitMQ -> идемпотентно, basic_ack се вика
    consumer2 = RabbitMQPaymentEventConsumer(session_factory=session_factory)
    ch2 = MagicMock()
    consumer2._handle_message(ch2, SimpleNamespace(delivery_tag=102), None, raw_body)
    ch2.basic_ack.assert_called_once_with(delivery_tag=102)

    with session_factory() as s:
        assert s.get(Order, order.id).status == "paid"  # type: ignore[union-attr]
        assert s.get(Product, product.id).stock == 7  # type: ignore[union-attr]
        assert (
            s.query(OrderOutboxEvent)
            .filter(OrderOutboxEvent.order_id == order.id)
            .count()
            == 1
        )


def test_concurrent_payment_event_deliveries_no_duplicate_outbox(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Паралелно прилагане на едно и също event_id минава без срив и дубликат."""
    user, _ = _create_user_and_token(db_session, email="concurrent_ev@example.com")
    product = _create_product(db_session, "RaceProd", price_minor=1500, stock=10)
    order = _create_order(db_session, user.id, product, quantity=1)

    ev = make_payment_event(
        event_type="payment.succeeded",
        order_id=order.id,
        amount_minor=1500,
    )

    def _run(_idx: int) -> str:
        with session_factory() as s:
            return _new_service(s).apply_payment_event(ev)

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(_run, range(4)))

    assert "paid" in outcomes
    assert all(o in ("paid", "already_processed") for o in outcomes)

    with session_factory() as s:
        assert s.get(Order, order.id).status == "paid"  # type: ignore[union-attr]
        assert (
            s.query(OrderOutboxEvent)
            .filter(OrderOutboxEvent.order_id == order.id)
            .count()
            == 1
        )


def test_processing_then_failed_rotates_attempt_key_and_ignores_stale_attempt(
    db_session: Session,
) -> None:
    """payment.processing -> payment.failed връща в pending_payment и отваря attempt_2."""
    user, _ = _create_user_and_token(db_session, email="rotate@example.com")
    product = _create_product(db_session, "AsyncFailProd", price_minor=2500, stock=10)
    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=25)
    order = _create_order(
        db_session, user.id, product, quantity=2, expires_at=future
    )  # 5000, stock=8

    svc = _new_service(db_session)
    assert (
        svc.apply_payment_event(
            make_payment_event(
                event_type="payment.processing",
                order_id=order.id,
                attempt_key=f"order_{order.id}_attempt_1",
                amount_minor=5000,
            )
        )
        == "processing"
    )
    db_session.refresh(order)
    assert order.status == "payment_processing"

    assert (
        svc.apply_payment_event(
            make_payment_event(
                event_type="payment.failed",
                order_id=order.id,
                attempt_key=f"order_{order.id}_attempt_1",
                amount_minor=5000,
            )
        )
        == "failed"
    )
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == "pending_payment"
    assert order.payment_attempt_seq == 2
    assert order.current_attempt_key == f"order_{order.id}_attempt_2"
    assert product.stock == 8

    # Закъсняло payment.processing / payment.expired за стария attempt_1 се игнорира!
    assert (
        svc.apply_payment_event(
            make_payment_event(
                event_type="payment.processing",
                order_id=order.id,
                attempt_key=f"order_{order.id}_attempt_1",
                amount_minor=5000,
            )
        )
        == "ignored_stale_attempt"
    )
    db_session.refresh(order)
    assert order.status == "pending_payment"
    assert order.current_attempt_key == f"order_{order.id}_attempt_2"


def test_failed_or_expired_after_reserve_deadline_expires_order_and_restores_stock(
    db_session: Session,
) -> None:
    """Терминален неуспех/изтичане след expires_at превежда поръчката в expired и връща stock."""
    user, _ = _create_user_and_token(db_session, email="exp_after@example.com")
    product = _create_product(db_session, "ExpAfterProd", price_minor=1500, stock=10)
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    order = _create_order(
        db_session,
        user.id,
        product,
        quantity=3,
        status="payment_processing",
        expires_at=past,
    )  # stock=7

    svc = _new_service(db_session)
    assert (
        svc.apply_payment_event(
            make_payment_event(
                event_type="payment.expired",
                order_id=order.id,
                attempt_key=f"order_{order.id}_attempt_1",
                amount_minor=4500,
            )
        )
        == "expired"
    )
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == "expired"
    assert product.stock == 10


def test_out_of_order_succeeded_before_stale_processing_stays_paid(
    db_session: Session,
) -> None:
    """Закъсняло payment.processing след payment.succeeded не връща поръчката назад от paid."""
    user, _ = _create_user_and_token(db_session, email="ooo@example.com")
    product = _create_product(db_session, "OOOProd", price_minor=1200, stock=5)
    order = _create_order(db_session, user.id, product, quantity=1)

    svc = _new_service(db_session)
    assert (
        svc.apply_payment_event(
            make_payment_event(
                event_type="payment.succeeded",
                order_id=order.id,
                amount_minor=1200,
            )
        )
        == "paid"
    )
    assert (
        svc.apply_payment_event(
            make_payment_event(
                event_type="payment.processing",
                order_id=order.id,
                amount_minor=1200,
            )
        )
        == "ignored"
    )
    db_session.refresh(order)
    assert order.status == "paid"


def test_late_payment_succeeded_on_cancelled_and_expired_orders_creates_visible_discrepancy(
    db_session: Session,
) -> None:
    """Късно payment.succeeded върху cancelled/expired НЕ променя статуса/stock/outbox, а записва видимо разхождение."""
    user, token = _create_user_and_token(db_session, email="disc_buyer@example.com")
    product = _create_product(db_session, "DiscProd", price_minor=2000, stock=10)

    cancelled_order = _create_order(
        db_session, user.id, product, quantity=2, status="cancelled"
    )  # 4000
    expired_order = _create_order(
        db_session, user.id, product, quantity=1, status="expired"
    )  # 2000
    # Симулираме вече върнат stock при отмяна/изтичане -> 10
    product.stock = 10
    db_session.commit()

    svc = _new_service(db_session)
    ev1 = make_payment_event(
        event_type="payment.succeeded",
        order_id=cancelled_order.id,
        amount_minor=4000,
        stripe_session_id="cs_disc_cancelled",
    )
    ev2 = make_payment_event(
        event_type="payment.succeeded",
        order_id=expired_order.id,
        amount_minor=2000,
        stripe_session_id="cs_disc_expired",
    )

    assert svc.apply_payment_event(ev1) == "discrepancy"
    assert svc.apply_payment_event(ev2) == "discrepancy"

    db_session.refresh(cancelled_order)
    db_session.refresh(expired_order)
    db_session.refresh(product)
    assert cancelled_order.status == "cancelled"
    assert expired_order.status == "expired"
    assert product.stock == 10
    assert (
        db_session.query(OrderOutboxEvent)
        .filter(OrderOutboxEvent.order_id.in_([cancelled_order.id, expired_order.id]))
        .count()
        == 0
    )

    d1 = (
        db_session.query(OrderPaymentDiscrepancy)
        .filter(OrderPaymentDiscrepancy.order_id == cancelled_order.id)
        .one()
    )
    assert d1.reason == "payment_on_cancelled_order"
    assert d1.amount_minor == 4000
    assert d1.status == "open"

    # Проверка през API на поръчката: видими has_payment_discrepancy и payment_discrepancies
    resp = client.get(
        f"/orders/{cancelled_order.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["has_payment_discrepancy"] is True
    assert len(body["payment_discrepancies"]) == 1
    assert body["payment_discrepancies"][0]["reason"] == "payment_on_cancelled_order"
    assert body["payment_discrepancies"][0]["amount_minor"] == 4000


def test_amount_mismatch_and_missing_order_are_safe(db_session: Session) -> None:
    """Несъответстваща сума или липсваща поръчка не променят статус и се записват в inbox."""
    user, _ = _create_user_and_token(db_session, email="mismatch@example.com")
    product = _create_product(db_session, "MismatchProd", price_minor=1500, stock=5)
    order = _create_order(db_session, user.id, product, quantity=1)

    svc = _new_service(db_session)
    assert (
        svc.apply_payment_event(
            make_payment_event(
                event_type="payment.succeeded",
                order_id=order.id,
                amount_minor=9999,
            )
        )
        == "mismatched"
    )
    db_session.refresh(order)
    assert order.status == "pending_payment"

    missing_ev = make_payment_event(
        event_type="payment.succeeded",
        order_id=999_999_999,
        amount_minor=1500,
    )
    assert svc.apply_payment_event(missing_ev) == "order_not_found"
    inbox = db_session.get(OrderInboxEvent, missing_ev.event_id)
    assert inbox is not None
    assert inbox.outcome == "order_not_found"


def test_payment_cancelled_completes_cancellation_and_ignores_duplicates_or_paid(
    db_session: Session,
) -> None:
    """payment.cancelled перевежда неплатена поръчка в cancelled, игнорира повторения и не пипа paid."""
    user, _ = _create_user_and_token(db_session, email="pcancel@example.com")
    product = _create_product(db_session, "PCancelProd", price_minor=1000, stock=10)
    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=25)

    # 1. Предхождащо payment.expired до края на резерва върти към attempt_2 (повторната оплата е позволена)
    # Но последващо payment.cancelled (дори със стария key attempt_1) финализира цялата поръчка като cancelled
    order_open = _create_order(
        db_session, user.id, product, quantity=2, expires_at=future
    )  # stock: 10 -> 8
    svc = _new_service(db_session)
    assert (
        svc.apply_payment_event(
            make_payment_event(
                event_type="payment.expired",
                order_id=order_open.id,
                attempt_key=f"order_{order_open.id}_attempt_1",
                amount_minor=2000,
            )
        )
        == "expired"
    )
    db_session.refresh(order_open)
    db_session.refresh(product)
    assert order_open.status == "pending_payment"
    assert order_open.current_attempt_key == f"order_{order_open.id}_attempt_2"
    assert product.stock == 8

    cancel_ev = make_payment_event(
        event_type="payment.cancelled",
        order_id=order_open.id,
        attempt_key=f"order_{order_open.id}_attempt_1",
        amount_minor=2000,
    )
    assert svc.apply_payment_event(cancel_ev) == "cancelled"
    db_session.refresh(order_open)
    db_session.refresh(product)
    assert order_open.status == "cancelled"
    assert product.stock == 10

    # Дубликат и ново payment.cancelled не връщат stock повторно
    assert svc.apply_payment_event(cancel_ev) == "already_processed"
    dup_cancel_ev = make_payment_event(
        event_type="payment.cancelled",
        order_id=order_open.id,
        attempt_key=f"order_{order_open.id}_attempt_1",
        amount_minor=2000,
    )
    assert svc.apply_payment_event(dup_cancel_ev) == "ignored"
    db_session.refresh(product)
    assert product.stock == 10

    # 2. payment_processing също се финализира като cancelled при потвърдена отмена
    order_proc = _create_order(
        db_session,
        user.id,
        product,
        quantity=1,
        status="payment_processing",
        expires_at=future,
    )  # stock: 10 -> 9
    assert (
        svc.apply_payment_event(
            make_payment_event(
                event_type="payment.cancelled",
                order_id=order_proc.id,
                attempt_key=f"order_{order_proc.id}_attempt_1",
                amount_minor=1000,
            )
        )
        == "cancelled"
    )
    db_session.refresh(order_proc)
    db_session.refresh(product)
    assert order_proc.status == "cancelled"
    assert product.stock == 10

    # 3. Вече платена поръчка НИКОГА не се превръща в cancelled и не връща stock
    order_paid = _create_order(
        db_session, user.id, product, quantity=3, expires_at=future
    )  # stock: 10 -> 7
    assert (
        svc.apply_payment_event(
            make_payment_event(
                event_type="payment.succeeded",
                order_id=order_paid.id,
                amount_minor=3000,
            )
        )
        == "paid"
    )
    assert (
        svc.apply_payment_event(
            make_payment_event(
                event_type="payment.cancelled",
                order_id=order_paid.id,
                amount_minor=3000,
            )
        )
        == "ignored"
    )
    db_session.refresh(order_paid)
    db_session.refresh(product)
    assert order_paid.status == "paid"
    assert product.stock == 7
