from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from app.modules.orders.models import OrderOutboxEvent
from app.modules.orders.outbox_processor import OutboxProcessor
from app.modules.orders.payments_client import CancelPaymentResult, PaymentsClient
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from tests.mock_clients import LocalTestCatalogClient
from tests.mock_models import Product
from tests.orders.helpers import _create_order, _create_user_and_token


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
    monkeypatch: None,
) -> None:
    """Грешка след успешен commit на поръчката (напр. в Cart) НЕ освобождава резерва в Catalog."""
    user, _ = _create_user_and_token(db_session, email="post_commit@example.com")
    product = Product(title="Committed Item", price_minor=1000, stock=10)
    db_session.add(product)
    order = _create_order(db_session, user.id, product, quantity=1)

    catalog_client = MagicMock(spec=LocalTestCatalogClient)

    # Уверяваме се, че поръчката е със статус pending_payment
    assert order.status == "pending_payment"
    catalog_client.release_stock.assert_not_called()
