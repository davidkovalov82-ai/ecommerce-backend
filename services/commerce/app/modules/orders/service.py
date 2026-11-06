import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.accounts.repository import UserRepository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.models import (
    Order,
    OrderItem,
    PaymentAttempt,
    StripeCheckoutSession,
    StripeProcessedEvent,
)
from app.modules.orders.repository import OrderRepository
from app.modules.orders.simulator import PaymentSimulator
from app.modules.orders.stripe_client import StripeClient, StripeNotConfigured


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


class OrderAlreadyPaid(Exception):
    """Сигнализира за опит за отмяна на вече платена поръчка."""


class OrderStateConflict(Exception):
    """Сигнализира за невалиден статус на поръчката при опит за преход."""

    def __init__(
        self, current_status: str, message: str = "Order status conflict"
    ) -> None:
        super().__init__(message)
        self.current_status = current_status


class IdempotencyConflict(Exception):
    """Сигнализира за повторно използване на същия idempotency key с различен метод."""


class InvalidOrderAmount(Exception):
    """Сигнализира за сума на поръчката извън допустимите граници за Stripe."""


class StripeServiceError(Exception):
    """Сигнализира за грешка при мрежова комуникация със Stripe API."""


class OrderService:
    """Услуга за управление на жизнения цикъл на поръчките."""

    def __init__(
        self,
        order_repository: OrderRepository,
        cart_repository: CartRepository,
        product_repository: ProductRepository,
        user_repository: UserRepository,
        session: Session,
        payment_simulator: PaymentSimulator | None = None,
        stripe_client: StripeClient | None = None,
    ) -> None:
        self._order_repository = order_repository
        self._cart_repository = cart_repository
        self._product_repository = product_repository
        self._user_repository = user_repository
        self._session = session
        self._payment_simulator = payment_simulator or PaymentSimulator()
        self._stripe_client = stripe_client or StripeClient()

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

            # Гарантираме с flush, че всички промени са изпратени към БД преди commit
            self._session.flush()

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

    def create_payment(
        self,
        order_id: int,
        user_id: int,
        idempotency_key: str,
        payment_method: str,
    ) -> tuple[PaymentAttempt, bool]:
        """Сценарий: симулирано плащане на поръчка с идемпотентност и заключване.

        Транзакционен протокол:
        1. Заключване на реда на поръчката с FOR UPDATE.
        2. Проверка за собственост и съществуване (OrderNotFound при чужда/липсваща).
        3. Проверка за съществуващ опит с този ключ ПРЕДИ проверка на статуса:
           - Различен payment_method -> 409 (IdempotencyConflict).
           - Същият payment_method -> 200 (връща съществуващия запис без повторен симулатор).
        4. Проверка на текущия статус на поръчката (само pending_payment се допуска).
        5. Извикване на симулатора:
           - succeeded -> статусът на поръчката става paid (без повторна промяна на stock).
           - declined -> поръчката остава pending_payment.
        6. Записване на PaymentAttempt.
        7. Flush и commit на транзакцията.
        """
        try:
            # 1. Заключваме реда на поръчката
            order = self._order_repository.get_by_id_and_user_id_for_update(
                order_id=order_id,
                user_id=user_id,
            )
            if order is None:
                raise OrderNotFound(f"Order with id {order_id} not found")

            # 2. Проверка за повторение ПРЕДИ проверка на статуса
            existing_attempt = (
                self._order_repository.get_payment_attempt_by_order_and_key(
                    order_id=order.id,
                    idempotency_key=idempotency_key,
                )
            )
            if existing_attempt is not None:
                if existing_attempt.payment_method != payment_method:
                    raise IdempotencyConflict(
                        "Idempotency key already used with different payment method"
                    )
                return existing_attempt, False

            # 3. Нов опит за плащане: поръчката трябва да е в pending_payment
            if order.status != "pending_payment":
                raise OrderStateConflict(
                    current_status=order.status,
                    message=f"Cannot pay order with status '{order.status}'",
                )

            # 4. Локален симулатор определя резултата
            sim_result = self._payment_simulator.simulate(payment_method)
            if sim_result == "succeeded":
                order.status = "paid"
                # Stock не се променя: количествата вече са резервирани при checkout

            # 5. Създаваме запис за опита
            attempt = PaymentAttempt(
                order_id=order.id,
                payment_method=payment_method,
                status=sim_result,
                amount_minor=order.total_minor,
                currency=order.currency,
                idempotency_key=idempotency_key,
            )
            self._order_repository.add_payment_attempt(attempt)
            self._session.flush()
            self._session.commit()
            self._session.refresh(attempt)
            return attempt, True
        except Exception:
            self._session.rollback()
            raise

    def _restore_order_stock(self, order: Order) -> None:
        """Възстановява запазените количества от позициите на поръчката в склада."""
        product_quantities: dict[int, int] = {}
        for item in order.items:
            product_quantities[item.product_id] = (
                product_quantities.get(item.product_id, 0) + item.quantity
            )

        sorted_product_ids = sorted(product_quantities.keys())
        locked_products = self._product_repository.get_by_ids_for_update(
            sorted_product_ids
        )
        products_by_id = {p.id: p for p in locked_products}

        for pid, qty in product_quantities.items():
            product = products_by_id.get(pid)
            if product is not None:
                product.stock += qty

    def create_stripe_checkout_session(
        self,
        order_id: int,
        user_id: int,
    ) -> tuple[str, str]:
        """Сценарий: създаване или извличане на активна Stripe Checkout сесия за собствена поръчка.

        Транзакционен протокол:
        1. Заключване на поръчката с FOR UPDATE.
        2. Проверка за собственост, съществуване и статус pending_payment.
        3. Проверка на сумата срещу лимитите на Stripe (мин. 50 цента EUR).
        4. Ако вече има отворена сесия с готов checkout_url -> връщаме я незабавно (без ново извикване към Stripe).
        5. Ако няма -> записваме опит в статус 'initiated' в БД, за да получим ID.
        6. Освобождаваме DB заключването и commit-ваме транзакцията преди мрежовата заявка към Stripe!
        7. Извикваме Stripe API с детерминиран idempotency_key = f"order_{order.id}_attempt_{attempt.id}".
        8. В нова кратка DB транзакция записваме върнатия stripe_session_id, checkout_url и статус 'open'.
        9. Връщаме (checkout_url, session_id).
        """
        # Фаза 1: Проверка и иницииране в БД
        try:
            order = self._order_repository.get_by_id_and_user_id_for_update(
                order_id=order_id,
                user_id=user_id,
            )
            if order is None:
                raise OrderNotFound(f"Order with id {order_id} not found")

            if order.status != "pending_payment":
                raise OrderStateConflict(
                    current_status=order.status,
                    message=f"Cannot pay order with status '{order.status}'",
                )

            # Минимална сума за Stripe в EUR е 50 цента (50 minor units)
            if order.total_minor < 50:
                raise InvalidOrderAmount(
                    "Order total must be at least 50 cents (EUR 0.50) for Stripe Checkout"
                )
            if order.total_minor > 99_999_999:
                raise InvalidOrderAmount(
                    "Order total exceeds Stripe's maximum allowed amount"
                )

            # Проверка за съществуваща сесия
            existing_session = (
                self._order_repository.get_open_or_initiated_stripe_session(order.id)
            )
            if (
                existing_session is not None
                and existing_session.status == "open"
                and existing_session.checkout_url
                and existing_session.stripe_session_id
            ):
                self._session.commit()
                return (
                    existing_session.checkout_url,
                    existing_session.stripe_session_id,
                )

            if existing_session is not None and existing_session.status == "initiated":
                attempt = existing_session
            else:
                attempt = StripeCheckoutSession(
                    order_id=order.id,
                    status="initiated",
                    amount_minor=order.total_minor,
                    currency=order.currency,
                    idempotency_key=f"order_{order.id}_pending_{uuid.uuid4().hex[:12]}",
                )
                self._order_repository.add_stripe_session(attempt)
                self._session.flush()
                attempt.idempotency_key = f"order_{order.id}_attempt_{attempt.id}"

            attempt_id = attempt.id
            idempotency_key = attempt.idempotency_key
            line_items = [
                {
                    "price_data": {
                        "currency": order.currency.lower(),
                        "unit_amount": item.unit_price_minor,
                        "product_data": {
                            "name": item.product_title,
                        },
                    },
                    "quantity": item.quantity,
                }
                for item in order.items
            ]
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        # Фаза 2: Мрежова заявка към Stripe БЕЗ отворена DB транзакция
        success_url = settings.stripe_success_url.format(order_id=order_id)
        cancel_url = settings.stripe_cancel_url.format(order_id=order_id)
        try:
            stripe_session = self._stripe_client.create_checkout_session(
                line_items=line_items,
                success_url=success_url,
                cancel_url=cancel_url,
                metadata={
                    "order_id": str(order_id),
                    "attempt_id": str(attempt_id),
                },
                client_reference_id=str(order_id),
                idempotency_key=idempotency_key,
            )
        except StripeNotConfigured:
            raise
        except Exception as e:
            raise StripeServiceError(f"Stripe session creation failed: {e}") from e

        # Фаза 3: Запис на Stripe резултата в БД
        try:
            attempt_record = self._order_repository.get_stripe_session_by_id(attempt_id)
            if attempt_record is not None and attempt_record.status != "completed":
                attempt_record.stripe_session_id = stripe_session.id
                attempt_record.checkout_url = stripe_session.url
                attempt_record.status = "open"
                self._session.commit()
            return stripe_session.url, stripe_session.id
        except Exception:
            self._session.rollback()
            raise

    def process_stripe_webhook(
        self, payload: bytes, sig_header: str | None
    ) -> dict[str, Any]:
        """Обработва входящо Stripe webhook събитие с проверка на подпис и идемпотентност."""
        # 1. Валидация на подписа на събитието
        event = self._stripe_client.construct_webhook_event(payload, sig_header)
        event_id = event["id"]
        event_type = event["type"]

        # 2. Идемпотентност: ако събитието вече е обработено, връщаме успех без повторни промени
        if self._order_repository.is_stripe_event_processed(event_id):
            return {"status": "already_processed", "event_id": event_id}

        # 3. Обработка на събитие checkout.session.completed
        if event_type == "checkout.session.completed":
            raw_obj = event["data"]["object"]
            session_data: dict[str, Any] = (
                raw_obj.to_dict() if hasattr(raw_obj, "to_dict") else dict(raw_obj)
            )
            payment_status = session_data.get("payment_status")
            order_id_raw = session_data.get("metadata", {}).get(
                "order_id"
            ) or session_data.get("client_reference_id")

            if not order_id_raw:
                # Записваме събитието като обработено, за да не се преповтаря
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                    )
                )
                self._session.commit()
                return {"status": "ignored", "reason": "no_order_id"}

            order_id = int(order_id_raw)
            order = self._order_repository.get_by_id_for_update(order_id)
            if order is None:
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                        order_id=None,
                    )
                )
                self._session.commit()
                return {"status": "order_not_found"}

            if payment_status != "paid":
                # Отказана карта или недовършено плащане: поръчката остава pending_payment
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                        order_id=order.id,
                    )
                )
                self._session.commit()
                return {"status": "payment_unpaid"}

            # Проверка на съвпадение на сума и валута
            amount_total = session_data.get("amount_total")
            currency = (session_data.get("currency") or "").upper()

            if amount_total != order.total_minor or currency != order.currency.upper():
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                        order_id=order.id,
                    )
                )
                self._session.commit()
                return {"status": "mismatched"}

            # Успешно плащане: маркираме поръчката като paid без повторно намаляване на наличности
            if order.status == "pending_payment":
                order.status = "paid"

            stripe_session_id = session_data.get("id")
            if stripe_session_id:
                s_rec = self._order_repository.get_stripe_session_by_stripe_id(
                    stripe_session_id
                )
                if s_rec is not None:
                    s_rec.status = "completed"

            self._order_repository.record_stripe_event(
                StripeProcessedEvent(
                    stripe_event_id=event_id,
                    event_type=event_type,
                    order_id=order.id,
                )
            )
            self._session.commit()
            return {"status": "success", "event_id": event_id}

        # Всички останали събития
        self._order_repository.record_stripe_event(
            StripeProcessedEvent(
                stripe_event_id=event_id,
                event_type=event_type,
            )
        )
        self._session.commit()
        return {"status": "ignored", "event_id": event_id}

    def cancel_order(self, order_id: int, user_id: int) -> Order:
        """Сценарий: отказ от неплатена поръчка, прекратяване на Stripe сесия и връщане на наличностите."""
        # 1. Заключваме реда на поръчката
        order = self._order_repository.get_by_id_and_user_id_for_update(
            order_id=order_id,
            user_id=user_id,
        )
        if order is None:
            raise OrderNotFound(f"Order with id {order_id} not found")

        # 2. Идемпотентен отговор при вече отказана поръчка
        if order.status == "cancelled":
            return order

        # 3. Отказ на платена поръчка е забранен
        if order.status == "paid":
            raise OrderAlreadyPaid("Cannot cancel paid order")

        if order.status != "pending_payment":
            raise OrderStateConflict(
                current_status=order.status,
                message=f"Cannot cancel order with status '{order.status}'",
            )

        # 4. Проверка за активна Stripe сесия
        open_session = self._order_repository.get_open_or_initiated_stripe_session(
            order.id
        )
        session_to_expire = None
        if (
            open_session is not None
            and open_session.stripe_session_id
            and open_session.status in ("open", "initiated")
        ):
            session_to_expire = open_session.stripe_session_id
            open_session.status = "cancelling"
            self._session.commit()

            # Прекратяваме Stripe сесията извън DB заключването
            try:
                self._stripe_client.expire_checkout_session(session_to_expire)
            except StripeNotConfigured:
                pass
            except Exception as e:
                err_msg = str(e).lower()
                if "already expired" in err_msg:
                    # Сесията вече е изтекла в Stripe; можем да продължим с отмяната
                    pass
                else:
                    # Ако Stripe не отговори, запазваме резервацията и позволяваме повторение
                    try:
                        s_rec = self._order_repository.get_stripe_session_by_stripe_id(
                            session_to_expire
                        )
                        if s_rec and s_rec.status == "cancelling":
                            s_rec.status = "open"
                            self._session.commit()
                    except SQLAlchemyError:
                        self._session.rollback()
                    raise StripeServiceError(
                        f"Failed to expire Stripe session: {e}"
                    ) from e

            # Повторно заключваме поръчката и проверяваме дали междувременно не е платена
            order = self._order_repository.get_by_id_and_user_id_for_update(
                order_id=order_id,
                user_id=user_id,
            )
            if order.status == "paid":
                # Плащането е спечелило състезанието с отказа: НЕ връщаме наличности!
                raise OrderAlreadyPaid(
                    "Payment completed before cancellation could finish"
                )

            if order.status == "cancelled":
                return order

            order.status = "cancelled"
            self._restore_order_stock(order)
            s_rec = self._order_repository.get_stripe_session_by_stripe_id(
                session_to_expire
            )
            if s_rec:
                s_rec.status = "expired"
            self._session.commit()
            self._session.refresh(order)
            _ = order.items
            return order

        # Няма активна Stripe сесия -> локална отмяна веднага
        if open_session is not None:
            open_session.status = "expired"
        order.status = "cancelled"
        self._restore_order_stock(order)
        self._session.commit()
        self._session.refresh(order)
        _ = order.items
        return order
