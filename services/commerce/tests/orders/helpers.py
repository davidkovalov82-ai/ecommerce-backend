import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app.modules.accounts.models import User
from app.modules.accounts.tokens import create_access_token
from app.modules.cart.models import CartItem
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderItem

DEFAULT_SHIPPING_ADDRESS = {
    "recipient_name": "Иван Иванов",
    "phone": "+359888123456",
    "country_code": "BG",
    "city": "София",
    "postal_code": "1000",
    "address_line1": "бул. Витоша 1",
    "address_line2": "ет. 2, ап. 5",
}

ORDER_PAYLOAD = {"shipping_address": DEFAULT_SHIPPING_ADDRESS}


def _create_user_and_token(
    db_session: Session,
    email: str = "customer@example.com",
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
    title: str = "Test Product",
    price_minor: int = 1000,
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


def _add_to_cart(
    db_session: Session,
    user_id: int,
    product_id: int,
    quantity: int = 1,
) -> None:
    """Директно добавя позиция в количката на потребителя."""
    cart_item = CartItem(
        user_id=user_id,
        product_id=product_id,
        quantity=quantity,
    )
    db_session.add(cart_item)
    db_session.commit()


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
