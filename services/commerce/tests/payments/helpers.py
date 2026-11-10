import datetime
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.modules.accounts.models import User
from app.modules.accounts.tokens import create_access_token
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderItem
from app.modules.orders.payment_contracts import ValidatedPaymentEvent


def _create_user_and_token(
    db_session: Session,
    email: str = "stripe_test_user@example.com",
    role: str = "customer",
) -> tuple[User, str]:
    """Създава тестов потребител и валиден Bearer токен."""
    user = User(
        email=email,
        password_hash="argon2_test_hash",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    token = create_access_token(user_id=user.id, secret=settings.jwt_secret)
    return user, token


def _create_product(
    db_session: Session,
    title: str = "Stripe Test Product",
    price_minor: int = 2500,
    stock: int = 10,
    active: bool = True,
) -> Product:
    """Създава тестов продукт в каталога."""
    product = Product(
        title=title,
        price_minor=price_minor,
        currency="EUR",
        stock=stock,
        active=active,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def _create_order(
    db_session: Session,
    user_id: int,
    product: Product,
    quantity: int = 2,
    status: str = "pending_payment",
    expires_at: datetime.datetime | None = None,
) -> Order:
    """Директно създава поръчка със запазена позиция и резервиран stock."""
    total_minor = product.price_minor * quantity
    order = Order(
        user_id=user_id,
        status=status,
        currency="EUR",
        total_minor=total_minor,
        idempotency_key=str(uuid.uuid4()),
        expires_at=expires_at,
    )
    db_session.add(order)
    db_session.flush()
    order.payment_attempt_seq = 1
    order.active_attempt_key = f"order_{order.id}_attempt_1"

    order_item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        product_title=product.title,
        quantity=quantity,
        unit_price_minor=product.price_minor,
        subtotal_minor=total_minor,
    )
    db_session.add(order_item)
    product.stock -= quantity
    db_session.commit()
    db_session.refresh(order)
    return order


def make_payment_event(
    *,
    event_type: str,
    order_id: int,
    payment_attempt_id: int = 101,
    attempt_key: str | None = None,
    amount_minor: int = 2500,
    currency: str = "EUR",
    stripe_session_id: str | None = "cs_test_1",
    stripe_event_id: str | None = None,
    event_id: str | None = None,
    occurred_at: datetime.datetime | None = None,
) -> ValidatedPaymentEvent:
    """Конструира валидирано платежно събитие от Payments към Commerce."""
    resolved_event_id = event_id or str(uuid.uuid4())
    resolved_attempt_key = attempt_key or f"order_{order_id}_attempt_1"
    resolved_stripe_event_id = stripe_event_id or f"evt_{uuid.uuid4().hex[:12]}"
    resolved_occurred_at = occurred_at or datetime.datetime.now(datetime.UTC)
    payload: dict[str, Any] = {
        "order_id": order_id,
        "payment_attempt_id": payment_attempt_id,
        "attempt_key": resolved_attempt_key,
        "stripe_session_id": stripe_session_id,
        "stripe_event_id": resolved_stripe_event_id,
        "amount_minor": amount_minor,
        "currency": currency.upper(),
        "mode": "test",
    }
    return ValidatedPaymentEvent(
        event_id=resolved_event_id,
        event_type=event_type,  # type: ignore[arg-type]
        event_version=1,
        occurred_at=resolved_occurred_at,
        order_id=order_id,
        payment_attempt_id=payment_attempt_id,
        attempt_key=resolved_attempt_key,
        stripe_session_id=stripe_session_id,
        stripe_event_id=resolved_stripe_event_id,
        amount_minor=amount_minor,
        currency=currency.upper(),
        mode="test",
        payload=payload,
    )


def make_payment_event_envelope(
    *,
    event_type: str,
    order_id: int,
    payment_attempt_id: int = 101,
    attempt_key: str | None = None,
    amount_minor: int = 2500,
    currency: str = "EUR",
    stripe_session_id: str | None = "cs_test_1",
    stripe_event_id: str | None = None,
    event_id: str | None = None,
    event_version: int = 1,
) -> dict[str, Any]:
    """Конструира суров JSON плик за RabbitMQ тестовете на платежния consumer."""
    resolved_event_id = event_id or str(uuid.uuid4())
    return {
        "event_id": resolved_event_id,
        "event_type": event_type,
        "event_version": event_version,
        "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "producer": "payments",
        "data": {
            "order_id": order_id,
            "payment_attempt_id": payment_attempt_id,
            "attempt_key": attempt_key or f"order_{order_id}_attempt_1",
            "stripe_session_id": stripe_session_id,
            "stripe_event_id": stripe_event_id or f"evt_{uuid.uuid4().hex[:12]}",
            "amount_minor": amount_minor,
            "currency": currency.upper(),
            "mode": "test",
        },
    }
