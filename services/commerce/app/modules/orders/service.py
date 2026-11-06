import contextlib
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.accounts.repository import UserRepository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.models import (
    Order,
    OrderItem,
    StripeCheckoutSession,
    StripeProcessedEvent,
)
from app.modules.orders.repository import OrderRepository
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
        stripe_client: StripeClient | None = None,
    ) -> None:
        self._order_repository = order_repository
        self._cart_repository = cart_repository
        self._product_repository = product_repository
        self._user_repository = user_repository
        self._session = session
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
        4. Проверка на съществуващи сесии:
           - open с checkout_url -> връщаме съществуващата сесия без ново повикване към Stripe.
           - cancelling -> конфликт (409): отказът е в ход, нов checkout не може да заобиколи защитата.
           - initiated -> резултатът още не е известен: преизползваме същия attempt и idempotency_key.
           - expired -> предишната е изтекла, създаваме нов опит.
        5. Записване/актуализиране на attempt в статус 'initiated' и commit преди мрежовото обръщение към Stripe.
        6. Извикване на Stripe API без отворена DB транзакция.
        7. В нова транзакция със заключване FOR UPDATE отразяваме резултата: закъснял отговор никога не връща completed или expired/cancelling сесия в open!
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

            # Проверка за съществуваща сесия с FOR UPDATE
            existing_session = (
                self._order_repository.get_latest_stripe_session_by_order_id_for_update(
                    order.id
                )
            )
            if existing_session is not None:
                if (
                    existing_session.status == "open"
                    and existing_session.checkout_url
                    and existing_session.stripe_session_id
                ):
                    self._session.commit()
                    return (
                        existing_session.checkout_url,
                        existing_session.stripe_session_id,
                    )

                if existing_session.status == "cancelling":
                    self._session.commit()
                    raise OrderStateConflict(
                        current_status="cancelling",
                        message="Cannot create checkout session while order cancellation is in progress",
                    )

                if existing_session.status == "completed":
                    self._session.commit()
                    raise OrderStateConflict(
                        current_status="paid",
                        message="Cannot create checkout session for paid order",
                    )

                if existing_session.status == "initiated":
                    # Резултатът още не е известен: преизползваме записа и същия idempotency_key
                    attempt = existing_session
                else:
                    # status == 'expired': създаваме нов опит
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

        # Фаза 3: Запис на Stripe резултата в БД със заключване FOR UPDATE
        try:
            attempt_record = self._order_repository.get_stripe_session_by_id_for_update(
                attempt_id
            )
            order_record = self._order_repository.get_by_id_for_update(order_id)

            if attempt_record is not None:
                attempt_record.stripe_session_id = stripe_session.id
                attempt_record.checkout_url = stripe_session.url

                # Закъснял отговор не трябва да върне completed или expired/cancelling сесия в open!
                if attempt_record.status == "completed" or (
                    order_record and order_record.status == "paid"
                ):
                    attempt_record.status = "completed"
                    self._session.commit()
                    return stripe_session.url, stripe_session.id

                if attempt_record.status in ("cancelling", "expired") or (
                    order_record and order_record.status == "cancelled"
                ):
                    attempt_record.status = "expired"
                    self._session.commit()
                    # Анулираме сесията в Stripe, тъй като поръчката е отказана
                    with contextlib.suppress(Exception):
                        self._stripe_client.expire_checkout_session(stripe_session.id)
                    raise OrderStateConflict(
                        current_status=order_record.status
                        if order_record
                        else "cancelled",
                        message="Order was cancelled while creating checkout session",
                    )

                if (
                    attempt_record.status == "initiated"
                    and order_record
                    and order_record.status == "pending_payment"
                ):
                    attempt_record.status = "open"
                    self._session.commit()
                    return stripe_session.url, stripe_session.id

            self._session.commit()
            return stripe_session.url, stripe_session.id
        except Exception:
            self._session.rollback()
            raise

    def process_stripe_webhook(
        self, payload: bytes, sig_header: str | None
    ) -> dict[str, Any]:
        """Обработва входящо Stripe webhook събитие с валидация на подпис, sandbox и точен attempt."""
        # 1. Валидация на подписа на събитието
        event_raw = self._stripe_client.construct_webhook_event(payload, sig_header)
        event: dict[str, Any] = (
            event_raw.to_dict() if hasattr(event_raw, "to_dict") else dict(event_raw)
        )
        event_id = event["id"]
        event_type = event["type"]

        # 2. Идемпотентност: ако събитието вече е обработено, връщаме успех без повторни промени
        if self._order_repository.is_stripe_event_processed(event_id):
            return {"status": "already_processed", "event_id": event_id}

        # 3. Sandbox проверка: отхвърляме livemode събития
        if event.get("livemode") is True:
            try:
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                    )
                )
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
            return {"status": "ignored", "reason": "livemode_not_supported"}

        # 4. Обработка само на checkout.session.completed
        if event_type != "checkout.session.completed":
            try:
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                    )
                )
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
            return {"status": "ignored", "event_id": event_id}

        raw_obj = event["data"]["object"]
        session_data: dict[str, Any] = (
            raw_obj.to_dict() if hasattr(raw_obj, "to_dict") else dict(raw_obj)
        )
        if session_data.get("livemode") is True:
            try:
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                    )
                )
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
            return {"status": "ignored", "reason": "livemode_not_supported"}

        # 5. Извличане на идентификатори
        stripe_session_id = session_data.get("id")
        metadata = session_data.get("metadata") or {}
        order_id_raw = metadata.get("order_id") or session_data.get(
            "client_reference_id"
        )
        attempt_id_raw = metadata.get("attempt_id")

        if not order_id_raw or not attempt_id_raw or not stripe_session_id:
            try:
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                    )
                )
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
            return {"status": "ignored", "reason": "missing_identifiers"}

        try:
            order_id = int(order_id_raw)
            attempt_id = int(attempt_id_raw)
        except ValueError:
            try:
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                    )
                )
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
            return {"status": "ignored", "reason": "invalid_identifiers"}

        # 6. Заключване на поръчката и опита в базата данни
        try:
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

            attempt_record = self._order_repository.get_stripe_session_by_id_for_update(
                attempt_id
            )
            if attempt_record is None or attempt_record.order_id != order.id:
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                        order_id=order.id,
                    )
                )
                self._session.commit()
                return {"status": "mismatched", "reason": "invalid_attempt"}

            # Ако webhook пристигне преди завършване на create_checkout_session:
            # свързваме сесията чрез вече записания локален опит
            if attempt_record.stripe_session_id is not None:
                if attempt_record.stripe_session_id != stripe_session_id:
                    self._order_repository.record_stripe_event(
                        StripeProcessedEvent(
                            stripe_event_id=event_id,
                            event_type=event_type,
                            order_id=order.id,
                        )
                    )
                    self._session.commit()
                    return {"status": "mismatched", "reason": "session_id_mismatch"}
            else:
                attempt_record.stripe_session_id = stripe_session_id

            # Проверка на payment_status
            payment_status = session_data.get("payment_status")
            if payment_status != "paid":
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
            if (
                amount_total != order.total_minor
                or amount_total != attempt_record.amount_minor
            ):
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                        order_id=order.id,
                    )
                )
                self._session.commit()
                return {"status": "mismatched", "reason": "amount_mismatch"}

            if (
                currency != order.currency.upper()
                or currency != attempt_record.currency.upper()
            ):
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                        order_id=order.id,
                    )
                )
                self._session.commit()
                return {"status": "mismatched", "reason": "currency_mismatch"}

            # Успешно плащане: маркираме поръчката като paid без повторно намаляване на stock
            if order.status == "pending_payment":
                order.status = "paid"

            attempt_record.status = "completed"

            self._order_repository.record_stripe_event(
                StripeProcessedEvent(
                    stripe_event_id=event_id,
                    event_type=event_type,
                    order_id=order.id,
                )
            )
            self._session.commit()
            return {"status": "success", "event_id": event_id}
        except IntegrityError:
            # Защита от паралелни доставки на един и същ event_id (връща 200 вече обработен, без 500)
            self._session.rollback()
            return {"status": "already_processed", "event_id": event_id}
        except Exception:
            self._session.rollback()
            raise

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

        # 4. Проверка за съществуваща Stripe сесия с FOR UPDATE
        latest_session = (
            self._order_repository.get_latest_stripe_session_by_order_id_for_update(
                order.id
            )
        )

        # А. Няма създавана Stripe сесия -> директен локален отказ
        if latest_session is None:
            order.status = "cancelled"
            self._restore_order_stock(order)
            self._session.commit()
            self._session.refresh(order)
            _ = order.items
            return order

        # Б. Сесията вече е завършена успешно
        if latest_session.status == "completed":
            self._session.commit()
            raise OrderAlreadyPaid("Cannot cancel paid order")

        # В. Сесията вече е изтекла
        if latest_session.status == "expired":
            order.status = "cancelled"
            self._restore_order_stock(order)
            self._session.commit()
            self._session.refresh(order)
            _ = order.items
            return order

        # Г. initiated без Stripe ID: резултатът още не е известен!
        # Докато резултатът е неизвестен, не връщаме stock и не приемаме сесията за прекратена!
        if (
            latest_session.status == "initiated"
            and not latest_session.stripe_session_id
        ):
            self._session.commit()
            raise OrderStateConflict(
                current_status="initiated",
                message="Payment session creation is in progress. Please retry once session is established.",
            )

        # Д. Активна или започнала прекратяване сесия с наличен Stripe ID
        session_to_expire = latest_session.stripe_session_id
        latest_session.status = "cancelling"
        self._session.commit()

        # Прекратяваме Stripe сесията извън DB заключването
        try:
            self._stripe_client.expire_checkout_session(session_to_expire)
        except StripeNotConfigured as e:
            # При липсващ Stripe ключ сесията НЕ се приема за прекратена!
            try:
                s_rec = self._order_repository.get_stripe_session_by_id_for_update(
                    latest_session.id
                )
                if s_rec and s_rec.status == "cancelling":
                    s_rec.status = "open"
                    self._session.commit()
            except SQLAlchemyError:
                self._session.rollback()
            raise StripeNotConfigured(str(e))
        except Exception as e:
            err_msg = str(e).lower()
            if "already expired" in err_msg:
                # Потвърдено от Stripe, че вече е изтекла
                pass
            else:
                # Timeout или неуспешно прекратяване: връщаме сесията в open и не пипаме stock!
                try:
                    s_rec = self._order_repository.get_stripe_session_by_id_for_update(
                        latest_session.id
                    )
                    if s_rec and s_rec.status == "cancelling":
                        s_rec.status = "open"
                        self._session.commit()
                except SQLAlchemyError:
                    self._session.rollback()
                raise StripeServiceError(f"Failed to expire Stripe session: {e}") from e

        # Повторно заключваме поръчката и проверяваме състезанието с webhook
        order = self._order_repository.get_by_id_and_user_id_for_update(
            order_id=order_id,
            user_id=user_id,
        )
        if order.status == "paid":
            # Плащането е спечелило състезанието с отказа: НЕ връщаме наличности!
            raise OrderAlreadyPaid("Payment completed before cancellation could finish")

        if order.status == "cancelled":
            return order

        order.status = "cancelled"
        self._restore_order_stock(order)
        s_rec = self._order_repository.get_stripe_session_by_id_for_update(
            latest_session.id
        )
        if s_rec:
            s_rec.status = "expired"
        self._session.commit()
        self._session.refresh(order)
        _ = order.items
        return order
