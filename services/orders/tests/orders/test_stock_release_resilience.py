import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.modules.catalog.dependencies import get_catalog_client
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
    """При грешка и rollback на транзакцията за отмяна, Catalog release_stock НЕ се извиква."""
    user, token = _create_user_and_token(
        db_session, email="rollback_cancel@example.com"
    )
    product = _create_product(db_session, stock=10)
    order = _create_order(db_session, user.id, product, quantity=2)
    assert order.status == "pending_payment"

    catalog_mock = MagicMock(wraps=shared_catalog_client)
    app.dependency_overrides[get_catalog_client] = lambda: catalog_mock

    # Симулираме срив при commit на транзакцията за отмяна
    def failing_commit(self: Session) -> None:
        raise RuntimeError("Controlled DB commit failure during cancellation")

    monkeypatch.setattr(Session, "commit", failing_commit)

    headers = {"Authorization": f"Bearer {token}"}
    with pytest.raises(
        RuntimeError, match="Controlled DB commit failure during cancellation"
    ):
        client.post(f"/orders/{order.id}/cancel", headers=headers)

    # Catalog release_stock НЕ трябва да е извикан, защото транзакцията претърпя rollback
    catalog_mock.release_stock.assert_not_called()

    # В базата поръчката остава в предишния статус
    with TestingSessionLocal() as session:
        refreshed_order = session.get(Order, order.id)
        assert refreshed_order is not None
        assert refreshed_order.status == "pending_payment"

        # Никакво публикувано release събитие в Outbox
        release_event = (
            session.query(OrderOutboxEvent)
            .filter(
                OrderOutboxEvent.order_id == order.id,
                OrderOutboxEvent.event_type == "order.stock_release",
            )
            .first()
        )
        assert release_event is None
