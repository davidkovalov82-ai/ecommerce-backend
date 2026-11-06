import datetime
from typing import Any
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from app.modules.accounts.repository import UserRepository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.models import StripeCheckoutSession
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from app.modules.orders.stripe_client import StripeClient
from tests.payments.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
)


def test_reconciliation_service_and_rules(db_session: Session) -> None:
    """Тества пълния набор от сценарии за периодичната сверка (scripts/reconcile_orders.py)."""
    user, _ = _create_user_and_token(db_session)
    product = _create_product(db_session, "Reconcile Item", stock=100, price_minor=1000)

    now = datetime.datetime.now(datetime.UTC)
    past = now - datetime.timedelta(minutes=40)

    # 1. Поръчка с изтекъл срок и БЕЗ Stripe опит -> expired, stock възстановен
    o1 = _create_order(db_session, user.id, product, quantity=2)  # stock 98
    o1.expires_at = past

    # 2. Поръчка с изтекъл срок и Stripe сесия 'open' -> потвърдено прекратяване в Stripe -> expired, stock възстановен
    o2 = _create_order(db_session, user.id, product, quantity=3)  # stock 95
    o2.expires_at = past
    s2 = StripeCheckoutSession(
        order_id=o2.id,
        status="open",
        amount_minor=3000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_open_2",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_open_2",
        idempotency_key="idemp_rec_2",
    )
    db_session.add(s2)

    # 3. Поръчка с изтекъл срок, но Stripe е недостъпен -> deferred, резервът се пази
    o3 = _create_order(db_session, user.id, product, quantity=1)  # stock 94
    o3.expires_at = past
    s3 = StripeCheckoutSession(
        order_id=o3.id,
        status="open",
        amount_minor=1000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_unavail_3",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_unavail_3",
        idempotency_key="idemp_rec_3",
    )
    db_session.add(s3)

    # 4. Поръчка в payment_processing (отложено плащане) -> deferred, резервът се пази дори при минал срок
    o4 = _create_order(db_session, user.id, product, quantity=4)  # stock 90
    o4.status = "payment_processing"
    o4.expires_at = past
    s4 = StripeCheckoutSession(
        order_id=o4.id,
        status="processing",
        amount_minor=4000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_proc_4",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_proc_4",
        idempotency_key="idemp_rec_4",
    )
    db_session.add(s4)

    # 5. Поръчка с изтекъл срок, но Stripe показва payment_status='paid' -> paid, опитът completed
    o5 = _create_order(db_session, user.id, product, quantity=1)  # stock 89
    o5.expires_at = past
    s5 = StripeCheckoutSession(
        order_id=o5.id,
        status="open",
        amount_minor=1000,
        currency="EUR",
        mode="test",
        stripe_session_id="cs_rec_paid_5",
        checkout_url="https://checkout.stripe.com/pay/cs_rec_paid_5",
        idempotency_key="idemp_rec_5",
    )
    db_session.add(s5)
    db_session.commit()

    # Mock StripeClient поведение за всяка сесия
    mock_stripe = MagicMock(spec=StripeClient)

    def mock_retrieve(session_id: str) -> Any:
        m = MagicMock()
        if session_id == "cs_rec_open_2":
            m.status = "open"
            m.payment_status = "unpaid"
            return m
        elif session_id == "cs_rec_unavail_3":
            raise RuntimeError("Stripe API connection timeout")
        elif session_id == "cs_rec_proc_4":
            m.status = "complete"
            m.payment_status = "unpaid"
            return m
        elif session_id == "cs_rec_paid_5":
            m.status = "complete"
            m.payment_status = "paid"
            return m
        raise ValueError(f"Unexpected session_id: {session_id}")

    mock_stripe.retrieve_checkout_session.side_effect = mock_retrieve

    order_repo = OrderRepository(db_session)
    cart_repo = CartRepository(db_session)
    product_repo = ProductRepository(db_session)
    user_repo = UserRepository(db_session)

    service = OrderService(
        order_repository=order_repo,
        cart_repository=cart_repo,
        product_repository=product_repo,
        user_repository=user_repo,
        session=db_session,
        stripe_client=mock_stripe,
    )

    stats = service.reconcile_unresolved_orders(now=now)

    assert stats["checked"] >= 5
    assert stats["expired"] >= 2  # o1 и o2
    assert stats["paid"] >= 1  # o5
    assert stats["deferred"] >= 2  # o3 и o4

    db_session.refresh(o1)
    db_session.refresh(o2)
    db_session.refresh(o3)
    db_session.refresh(o4)
    db_session.refresh(o5)

    assert o1.status == "expired"
    assert o2.status == "expired"
    assert o3.status == "pending_payment"
    assert o4.status == "payment_processing"
    assert o5.status == "paid"

    # Повторна сверка не променя нищо допълнително (идемпотентност)
    stats2 = service.reconcile_unresolved_orders(now=now)
    assert stats2["expired"] == 0

