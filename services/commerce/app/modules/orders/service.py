from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.modules.accounts.repository import UserRepository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.models import Order, OrderItem
from app.modules.orders.repository import OrderRepository


class CartEmpty(Exception):
    """Сигнализира, че потребителската количка е празна при опит за поръчка."""


class CartProductUnavailable(Exception):
    """Сигнализира, че продукт от количката липсва или е деактивиран."""

    def __init__(self, product_id: int, message: str = "Product unavailable") -> None:
        super().__init__(message)
        self.product_id = product_id


class InsufficientStock(Exception):
    """Сигнализира, че наличността на даден продукт не достига за заявеното количество."""

    def __init__(self, product_id: int, message: str = "Insufficient stock") -> None:
        super().__init__(message)
        self.product_id = product_id


class OrderNotFound(Exception):
    """Сигнализира, че търсената поръчка не съществува или принадлежи на друг потребител."""


class OrderService:
    """Услуга за управление на жизнения цикъл на поръчките."""

    def __init__(
        self,
        order_repository: OrderRepository,
        cart_repository: CartRepository,
        product_repository: ProductRepository,
        user_repository: UserRepository,
        session: Session,
    ) -> None:
        self._order_repository = order_repository
        self._cart_repository = cart_repository
        self._product_repository = product_repository
        self._user_repository = user_repository
        self._session = session

    def create_order(self, user_id: int, idempotency_key: str) -> Order:
        """Сценарий: оформяне на цялата текуща количка в нова поръчка.

        Транзакционен протокол:
        1. Заключване на потребителя с FOR NO KEY UPDATE за пълна сериализация.
        2. Проверка за съществуваща поръчка със същия ключ (идемпотентност).
        3. Проверка за непразна количка.
        4. Заключване на засегнатите продукти във възходящ ред по ID с FOR UPDATE.
        5. Проверка на актуална активност и наличност.
        6. Създаване на Order и исторически OrderItem позиции.
        7. Намаляване на складовите наличности.
        8. Изчистване на потребителската количка.
        9. Commit на транзакцията.
        """
        try:
            # 1. Обща DB блокировка за текущия потребител
            self._user_repository.lock_by_id(user_id)

            # 2. Проверка за съществуваща поръчка със същия ключ (идемпотентност преди количката)
            existing_order = self._order_repository.get_by_user_and_idempotency_key(
                user_id=user_id,
                idempotency_key=idempotency_key,
            )
            if existing_order is not None:
                # Връща съществуващата поръчка без да променя склад или количка
                return existing_order

            # 3. Прочитане на текущата количка
            cart_items = self._cart_repository.list_by_user_id(user_id)
            if not cart_items:
                raise CartEmpty("Shopping cart is empty")

            # 4. Заключване на продуктите във възходящ ред по id с гаранция за свежи стойности
            product_ids = [item.product_id for item in cart_items]
            locked_products = self._product_repository.get_by_ids_for_update(
                product_ids
            )
            products_by_id = {p.id: p for p in locked_products}

            # 5. Проверка на актуалните active, stock и price_minor
            order_items_data: list[tuple[int, str, int, int, int]] = []
            total_minor = 0

            for item in cart_items:
                product = products_by_id.get(item.product_id)
                if product is None or not product.active:
                    raise CartProductUnavailable(item.product_id)

                if product.stock < item.quantity:
                    raise InsufficientStock(item.product_id)

                unit_price = product.price_minor
                subtotal = unit_price * item.quantity
                total_minor += subtotal
                order_items_data.append(
                    (
                        product.id,
                        product.title,
                        item.quantity,
                        unit_price,
                        subtotal,
                    )
                )

            # 6. Създаване на поръчката и запазените позиции
            order = Order(
                user_id=user_id,
                status="pending_payment",
                currency="EUR",
                total_minor=total_minor,
                idempotency_key=idempotency_key,
            )
            self._order_repository.add(order)
            self._session.flush()

            for (
                p_id,
                p_title,
                qty,
                u_price,
                subtot,
            ) in order_items_data:
                order_item = OrderItem(
                    order_id=order.id,
                    product_id=p_id,
                    product_title=p_title,
                    quantity=qty,
                    unit_price_minor=u_price,
                    subtotal_minor=subtot,
                )
                self._order_repository.add_item(order_item)

                # 7. Намаляване на наличността с поръчаното количество
                product = products_by_id[p_id]
                product.stock -= qty

            # 8. Изчистване на поръчаната количка
            self._cart_repository.clear_by_user_id(user_id)

            # 9. Commit
            self._session.commit()
            self._session.refresh(order)
            _ = order.items
            return order
        except Exception:
            self._session.rollback()
            raise

    def get_order(self, order_id: int, user_id: int) -> Order:
        """Сценарий: извличане на собствена поръчка с нейните позиции."""
        order = self._order_repository.get_by_id_and_user_id(
            order_id=order_id,
            user_id=user_id,
        )
        if order is None:
            raise OrderNotFound(f"Order with id {order_id} not found")
        return order

    def list_orders(
        self,
        user_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[Sequence[Order], int]:
        """Сценарий: извличане на пагиниран списък с поръчки на потребителя."""
        return self._order_repository.list_by_user_id(
            user_id=user_id,
            limit=limit,
            offset=offset,
        )
