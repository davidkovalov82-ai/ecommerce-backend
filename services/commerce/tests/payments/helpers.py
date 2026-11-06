import hashlib
import hmac
import time
import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app.modules.accounts.models import User
from app.modules.accounts.tokens import create_access_token
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderItem

TEST_WEBHOOK_SECRET = "whsec_test_secret_stripe_commerce_32_bytes_ok"


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
) -> Order:
    """Директно създава поръчка със запазена позиция и резервиран stock."""
    total_minor = product.price_minor * quantity
    order = Order(
        user_id=user_id,
        status=status,
        currency="EUR",
        total_minor=total_minor,
        idempotency_key=str(uuid.uuid4()),
    )
    db_session.add(order)
    db_session.flush()

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


def _generate_stripe_signature(
    payload_bytes: bytes,
    secret: str = TEST_WEBHOOK_SECRET,
    timestamp: int | None = None,
) -> str:
    """Генерира валиден Stripe-Signature хедър чрез HMAC SHA256."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode() + payload_bytes
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"

