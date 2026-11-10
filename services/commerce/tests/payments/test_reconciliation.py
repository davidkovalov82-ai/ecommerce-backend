import datetime
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from app.modules.accounts.repository import UserRepository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.payments_client import (
    CancelPaymentResult,
    PaymentGatewayError,
    PaymentsClient,
    RemoteOrderAlreadyPaid,
    RemotePaymentConflict,
)
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from tests.payments.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
)


def test_reconciliation_service_and_rules(db_session: Session) -> None:
    """Тества сверката на просрочени поръчки в Commerce с Payments (без директно Stripe API)."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Reconcile Item", stock=100, price_minor=1000)

    now = datetime.datetime.now(datetime.UTC)
    past = now - datetime.timedelta(minutes=40)
    future = now + datetime.timedelta(minutes=20)

    # 1. Просрочена -> Payments потвърждава can_cancel -> expired + stock възстановен
    o1 = _create_order(db_session, user.id, product, quantity=2, expires_at=past)  # 98

    # 2. Просрочена -> Payments потвърждава already_expired -> expired + stock възстановен
    o2 = _create_order(db_session, user.id, product, quantity=3, expires_at=past)  # 95

    # 3. Просрочена -> Payments недостъпен -> deferred, резервът се пази
    o3 = _create_order(db_session, user.id, product, quantity=1, expires_at=past)  # 94

    # 4. Просрочена в payment_processing -> Payments връща конфликт -> deferred, резервът се пази
    o4 = _create_order(
        db_session,
        user.id,
        product,
        quantity=4,
        status="payment_processing",
        expires_at=past,
    )  # 90

    # 5. Просрочена -> Payments показва already paid -> deferred (чака payment.succeeded), резервът се пази
    o5 = _create_order(db_session, user.id, product, quantity=1, expires_at=past)  # 89

    # 6. Активна непросрочена -> не се проверява
    o6 = _create_order(
        db_session, user.id, product, quantity=2, expires_at=future
    )  # 87

    mock_payments = MagicMock(spec=PaymentsClient)

    def mock_cancel(*, order_id: int, **_kwargs):  # type: ignore[no-untyped-def]
        if order_id == o1.id:
            return CancelPaymentResult(status="can_cancel", action="no_session")
        if order_id == o2.id:
            return CancelPaymentResult(
                status="already_expired", action="already_expired"
            )
        if order_id == o3.id:
            raise PaymentGatewayError("Payments down")
        if order_id == o4.id:
            raise RemotePaymentConflict("processing", "Async payment in progress")
        if order_id == o5.id:
            raise RemoteOrderAlreadyPaid("Order is paid in Payments")
        raise AssertionError(f"Unexpected order_id {order_id}")

    mock_payments.cancel_order_payment.side_effect = mock_cancel

    service = OrderService(
        order_repository=OrderRepository(db_session),
        cart_repository=CartRepository(db_session),
        product_repository=ProductRepository(db_session),
        user_repository=UserRepository(db_session),
        session=db_session,
        payments_client=mock_payments,
    )
    stats = service.reconcile_expired_orders(now=now)
    assert stats["checked"] == 5
    assert stats["expired"] == 2
    assert stats["deferred"] == 3
    assert stats["paid"] == 0

    db_session.refresh(o1)
    db_session.refresh(o2)
    db_session.refresh(o3)
    db_session.refresh(o4)
    db_session.refresh(o5)
    db_session.refresh(o6)
    db_session.refresh(product)

    assert o1.status == "expired"
    assert o2.status == "expired"
    assert o3.status == "pending_payment"
    assert o4.status == "payment_processing"
    assert o5.status == "pending_payment"
    assert o6.status == "pending_payment"
    # Първоначално 100 - 13 (всички) + 2 (o1) + 3 (o2) = 92
    assert product.stock == 92

    # Идемпотентност: повторната сверка не пипа o1/o2 и пази stock=92
    stats2 = service.reconcile_expired_orders(now=now)
    assert stats2["checked"] == 3
    assert stats2["expired"] == 0
    assert stats2["deferred"] == 3
    db_session.refresh(product)
    assert product.stock == 92
