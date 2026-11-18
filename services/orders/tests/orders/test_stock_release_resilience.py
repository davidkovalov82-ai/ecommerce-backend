import datetime
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.modules.catalog.dependencies import get_catalog_client
from app.modules.orders.dependencies import get_payments_client
from app.modules.orders.models import Order, OrderOutboxEvent
from app.modules.orders.outbox_processor import OutboxProcessor
from app.modules.orders.payments_client import CancelPaymentResult, PaymentsClient
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from tests.conftest import TestingSessionLocal
from tests.mock_clients import LocalTestCatalogClient, shared_catalog_client
from tests.mock_models import Product
from tests.orders.helpers import (
    ORDER_PAYLOAD,
    _add_to_cart,
    _create_order,
    _create_product,
    _create_user_and_token,
)

client = TestClient(app)


def test_stock_release_outbox_persistence_and_retry(db_session: Session) -> None:
    """При отмяна на поръчка:

    1. Ако незабавното освобождаване към CatalogClient хвърли грешка,
       намерението order.stock_release остава записано в Outbox като 'pending'.
    2. OutboxProcessor повторно изпълнява release_stock към Catalog и го отбелязва като 'published'.
    """
    user, _ = _create_user_and_token(db_session, email="resilient_cancel@example.com")
    product = Product(
        title="Resilient Item",
        price_minor=2000,
        currency="EUR",
        stock=5,
        active=True,
    )
    db_session.add(product)
    order = _create_order(db_session, user.id, product, quantity=2)

    failing_catalog = MagicMock(spec=LocalTestCatalogClient)
    failing_catalog.release_stock.side_effect = RuntimeError(
        "Catalog is temporarily down"
    )

    mock_payments = MagicMock(spec=PaymentsClient)
    mock_payments.cancel_order_payment.return_value = CancelPaymentResult(
        status="can_cancel", action="no_session"
    )

    service = OrderService(
        order_repository=OrderRepository(db_session),
        session=db_session,
        payments_client=mock_payments,
        catalog_client=failing_catalog,
    )

    # 1. Отказваме поръчката: Catalog е недостъпен, но транзакцията успява
    service.cancel_order(order.id, user.id)

    db_session.refresh(order)
    assert order.status == "cancelled"

    outbox_event = (
        db_session.query(OrderOutboxEvent)
        .filter(
            OrderOutboxEvent.order_id == order.id,
            OrderOutboxEvent.event_type == "order.stock_release",
        )
        .first()
    )
    assert outbox_event is not None
    assert outbox_event.status == "pending"

    # 2. Възстановяваме Catalog сервиза и пускаме OutboxProcessor
    healthy_catalog = MagicMock(spec=LocalTestCatalogClient)
    processor = OutboxProcessor(
        session_factory=lambda: db_session,
        catalog_client=healthy_catalog,
    )

    stats = processor.process_pending_events(limit=10)
    assert stats["processed"] >= 1

    updated_event = (
        db_session.query(OrderOutboxEvent)
        .filter(OrderOutboxEvent.event_id == outbox_event.event_id)
        .first()
    )
    assert updated_event is not None
    assert updated_event.status == "published"
    assert updated_event.published_at is not None
    healthy_catalog.release_stock.assert_called_once_with(order_id=order.id)


def test_post_commit_error_does_not_release_reserve(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Грешка след успешен commit на поръчката (напр. в refresh) НЕ освобождава резерва в Catalog."""
    user, token = _create_user_and_token(db_session, email="post_commit@example.com")
    product = _create_product(db_session, stock=10, price_minor=1000)
    _add_to_cart(db_session, user.id, product.id, quantity=2)

    catalog_mock = MagicMock(wraps=shared_catalog_client)
    app.dependency_overrides[get_catalog_client] = lambda: catalog_mock

    orig_refresh = Session.refresh
    refresh_called = False

    def failing_refresh(self: Session, instance: Any, **kwargs: Any) -> None:
        nonlocal refresh_called
        if isinstance(instance, Order):
            refresh_called = True
            raise RuntimeError("Controlled post-commit refresh error")  # noqa: TRY004
        orig_refresh(self, instance, **kwargs)

    monkeypatch.setattr(Session, "refresh", failing_refresh)

    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
    }

    with pytest.raises(RuntimeError, match="Controlled post-commit refresh error"):
        client.post("/orders", json=ORDER_PAYLOAD, headers=headers)

    assert refresh_called is True
    catalog_mock.reserve_stock.assert_called_once()
    catalog_mock.release_stock.assert_not_called()

    with TestingSessionLocal() as session:
        saved_order = session.query(Order).filter(Order.user_id == user.id).first()
        assert saved_order is not None
        assert saved_order.status == "pending_payment"


def test_cancel_order_rollback_does_not_release_stock(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """При грешка и rollback на транзакцията за отмяна, Catalog release_stock НЕ се извиква.

    1. Първият подготвителен commit (Фаза 1, освобождаване на заключването) преминава успешно.
    2. Грешката се предизвиква при commit във Фаза 3, който записва cancelled статус и outbox.
    3. Потвърждаваме достигането на тази фаза, rollback на статуса и outbox, липса на Catalog release.
    """
    user, token = _create_user_and_token(
        db_session, email="rollback_cancel@example.com"
    )
    product = _create_product(db_session, stock=10)
    order = _create_order(db_session, user.id, product, quantity=2)
    assert order.status == "pending_payment"

    catalog_mock = MagicMock(wraps=shared_catalog_client)
    app.dependency_overrides[get_catalog_client] = lambda: catalog_mock

    payments_mock = MagicMock(spec=PaymentsClient)
    payments_mock.cancel_order_payment.return_value = CancelPaymentResult(
        status="can_cancel", action="cancelled"
    )
    app.dependency_overrides[get_payments_client] = lambda: payments_mock

    orig_commit = Session.commit
    commit_call_count = 0
    phase_3_commit_reached = False

    def controlled_commit(self: Session) -> None:
        nonlocal commit_call_count, phase_3_commit_reached
        commit_call_count += 1
        # Първият подготвителен commit (Фаза 1) трябва да премине успешно
        if commit_call_count == 1:
            orig_commit(self)
            return

        # Вторият commit (Фаза 3) записва cancelled статус и outbox
        # Потвърждаваме достигането на фазата чрез обектите в сесията преди commit
        for obj in list(self.dirty) + list(self.new):
            if isinstance(obj, Order) and obj.status == "cancelled":
                phase_3_commit_reached = True
                break
        raise RuntimeError("Controlled DB commit failure during cancellation phase 3")

    monkeypatch.setattr(Session, "commit", controlled_commit)

    headers = {"Authorization": f"Bearer {token}"}
    with pytest.raises(
        RuntimeError, match="Controlled DB commit failure during cancellation phase 3"
    ):
        client.post(f"/orders/{order.id}/cancel", headers=headers)

    # 1. Потвърждаваме преминаването на подготвителния commit и достигането на фаза 3
    assert commit_call_count == 2
    assert phase_3_commit_reached is True

    # 2. Catalog release_stock НЕ трябва да е извикан, защото транзакцията претърпя rollback
    catalog_mock.release_stock.assert_not_called()

    # 3. В базата поръчката претърпява rollback към 'pending_payment'
    with TestingSessionLocal() as session:
        refreshed_order = session.get(Order, order.id)
        assert refreshed_order is not None
        assert refreshed_order.status == "pending_payment"

        # 4. Никакво release събитие в Outbox
        release_event = (
            session.query(OrderOutboxEvent)
            .filter(
                OrderOutboxEvent.order_id == order.id,
                OrderOutboxEvent.event_type == "order.stock_release",
            )
            .first()
        )
        assert release_event is None


def test_create_checkout_session_order_paid_during_expiration_does_not_release_stock(
    db_session: Session,
) -> None:
    """Ако между фазите на create_stripe_checkout_session поръчката стане paid:

    1. Повторното четене открива статус paid.
    2. Резервът в Catalog НЕ се освобождава.
    3. Не се записва/публикува stock_release outbox събитие.
    4. Заявката връща 409 Conflict със статус paid.
    """
    user, token = _create_user_and_token(
        db_session, email="expire_paid_race@example.com"
    )
    product = _create_product(db_session, stock=10)
    order = _create_order(db_session, user.id, product, quantity=2)

    # Задаваме expires_at в миналото, за да задействаме клона за изтичане
    order.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        seconds=10
    )
    db_session.commit()

    catalog_mock = MagicMock(wraps=shared_catalog_client)
    app.dependency_overrides[get_catalog_client] = lambda: catalog_mock

    # Симулираме, че по време на Фаза 2 (cancel_order_payment) поръчката е платена паралелно
    payments_mock = MagicMock(spec=PaymentsClient)

    def fake_cancel_order_payment(
        order_id: int,
        reason: str,
        order_expires_at: Any,
    ) -> CancelPaymentResult:
        with TestingSessionLocal() as session:
            o = session.get(Order, order_id)
            assert o is not None
            o.status = "paid"
            session.commit()
        return CancelPaymentResult(status="can_cancel", action="cancelled")

    payments_mock.cancel_order_payment.side_effect = fake_cancel_order_payment
    app.dependency_overrides[get_payments_client] = lambda: payments_mock

    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(f"/orders/{order.id}/checkout-session", headers=headers)

    # 1. Повторното четене открива paid -> 409 с код ORDER_STATUS_CONFLICT
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"
    assert "paid" in resp.json()["detail"]["message"]

    # 2. Резервът в Catalog НЕ е освободен
    catalog_mock.release_stock.assert_not_called()

    # 3. Поръчката остава със статус paid в базата
    with TestingSessionLocal() as session:
        refreshed_order = session.get(Order, order.id)
        assert refreshed_order is not None
        assert refreshed_order.status == "paid"

        # 4. Не е записано никакво stock_release outbox събитие
        release_event = (
            session.query(OrderOutboxEvent)
            .filter(
                OrderOutboxEvent.order_id == order.id,
                OrderOutboxEvent.event_type == "order.stock_release",
            )
            .first()
        )
        assert release_event is None
