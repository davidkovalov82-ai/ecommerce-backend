import datetime
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
    OrderOutboxEvent,
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

            # 5. Проверка на актуалните active, stock и price_minor, както и статуса на продавача
            seller_ids = [p.seller_id for p in locked_products]
            sellers = self._user_repository.get_by_ids(seller_ids)
            sellers_by_id = {u.id: u for u in sellers}

            order_items_data: list[tuple[int, str, int, int, int]] = []
            total_minor = 0

            for item in cart_items:
                product = products_by_id.get(item.product_id)
                if product is None or not product.active:
                    raise CartProductUnavailable(item.product_id)

                seller = sellers_by_id.get(product.seller_id)
                if seller is None or seller.status != "active":
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
            now = datetime.datetime.now(datetime.UTC)
            expires_at = now + datetime.timedelta(
                minutes=settings.order_reserve_timeout_minutes
            )
            order = Order(
                user_id=user_id,
                status="pending_payment",
                currency="EUR",
                total_minor=total_minor,
                idempotency_key=idempotency_key,
                expires_at=expires_at,
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

    def _transition_order_to_paid(self, order: Order) -> None:
        """Превежда поръчката в статус paid и записва outbox събитие order.paid в същата транзакция.

        Ако събитие order.paid за тази поръчка вече съществува (напр. повторен webhook или сверка),
        не се създава нов дубликат.
        """
        if order.status != "paid":
            order.status = "paid"
        self._record_order_paid_outbox_event(order)

    def _record_order_paid_outbox_event(self, order: Order) -> None:
        """Записва order.paid събитие в outbox таблицата в същата PostgreSQL транзакция."""
        existing = self._order_repository.get_outbox_event(order.id, "order.paid")
        if existing is not None:
            return

        user = self._user_repository.get_by_id(order.user_id)
        customer_email = user.email if user else ""

        payload = {
            "order_id": order.id,
            "customer_email": customer_email,
            "total_minor": order.total_minor,
            "currency": order.currency,
            "created_at": order.created_at.isoformat() if order.created_at else None,
        }
        event = OrderOutboxEvent(
            order_id=order.id,
            event_type="order.paid",
            payload=payload,
            status="pending",
        )
        self._order_repository.record_outbox_event(event)

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

            if order.status == "payment_processing":
                raise OrderStateConflict(
                    current_status="payment_processing",
                    message="Cannot create checkout session while payment is processing",
                )

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

            now = datetime.datetime.now(datetime.UTC)
            if order.expires_at and order.expires_at <= now:
                if (
                    existing_session
                    and existing_session.status == "open"
                    and existing_session.stripe_session_id
                ):
                    session_to_expire = existing_session.stripe_session_id
                    existing_session.status = "cancelling"
                    self._session.commit()
                    try:
                        self._stripe_client.expire_checkout_session(session_to_expire)
                    except Exception:  # noqa: BLE001, S110
                        pass
                    order_rec = self._order_repository.get_by_id_for_update(order.id)
                    s_rec = self._order_repository.get_stripe_session_by_id_for_update(
                        existing_session.id
                    )
                    if order_rec and order_rec.status not in (
                        "cancelled",
                        "expired",
                        "paid",
                    ):
                        order_rec.status = "expired"
                        if s_rec:
                            s_rec.status = "expired"
                        self._restore_order_stock(order_rec)
                    self._session.commit()
                else:
                    order.status = "expired"
                    if existing_session and existing_session.status != "expired":
                        existing_session.status = "expired"
                    self._restore_order_stock(order)
                    self._session.commit()
                raise OrderStateConflict(
                    current_status="expired",
                    message="Order reserve has expired",
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

                if existing_session.status in ("cancelling", "processing"):
                    self._session.commit()
                    raise OrderStateConflict(
                        current_status=existing_session.status,
                        message=f"Cannot create checkout session while session is in status '{existing_session.status}'",
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
                    # status in ('expired', 'failed'): създаваме нов опит
                    attempt = StripeCheckoutSession(
                        order_id=order.id,
                        status="initiated",
                        amount_minor=order.total_minor,
                        currency=order.currency,
                        mode=settings.stripe_mode,
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
                    mode=settings.stripe_mode,
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

        # Фаза 3: Запис на Stripe резултата в БД със заключване FOR UPDATE в ред: заказ -> попытка
        try:
            order_record = self._order_repository.get_by_id_for_update(order_id)
            attempt_record = self._order_repository.get_stripe_session_by_id_for_update(
                attempt_id
            )

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
                    order_record and order_record.status in ("cancelled", "expired")
                ):
                    # Отказът или изтичането са настъпили, докато чакахме Stripe.
                    # Не записваме expired преди реално потвърждение от Stripe!
                    attempt_record.status = "cancelling"
                    self._session.commit()

                    # Прекратяваме сесията в Stripe БЕЗ задържане на DB заключване и БЕЗ заглушаване на грешки
                    try:
                        self._stripe_client.expire_checkout_session(stripe_session.id)
                    except Exception as e:
                        raise StripeServiceError(
                            f"Failed to confirm termination of late session in Stripe: {e}"
                        ) from e

                    # След като прекратяването е потвърдено в Stripe, отразяваме в БД
                    order_record = self._order_repository.get_by_id_for_update(order_id)
                    attempt_record = (
                        self._order_repository.get_stripe_session_by_id_for_update(
                            attempt_id
                        )
                    )
                    if attempt_record is not None:
                        attempt_record.status = "expired"
                    self._session.commit()

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
        """Обработва входящо Stripe webhook събитие с валидация на подпис, режим и точен опит."""
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

        # 3. Режим на работа (sandbox vs live)
        expected_livemode = settings.stripe_mode == "live"
        if event.get("livemode") != expected_livemode:
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
                if self._order_repository.is_stripe_event_processed(event_id):
                    return {"status": "already_processed", "event_id": event_id}
                raise
            return {"status": "ignored", "reason": "mode_mismatch"}

        # 4. Филтриране само на поддържаните 4 типа събития
        supported_events = {
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed",
            "checkout.session.expired",
        }
        if event_type not in supported_events:
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
                if self._order_repository.is_stripe_event_processed(event_id):
                    return {"status": "already_processed", "event_id": event_id}
                raise
            return {"status": "ignored", "event_id": event_id}

        raw_obj = event.get("data", {}).get("object", {})
        session_data: dict[str, Any] = (
            raw_obj.to_dict() if hasattr(raw_obj, "to_dict") else dict(raw_obj)
        )
        if "livemode" in session_data and session_data["livemode"] != expected_livemode:
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
                if self._order_repository.is_stripe_event_processed(event_id):
                    return {"status": "already_processed", "event_id": event_id}
                raise
            return {"status": "ignored", "reason": "mode_mismatch"}

        # 5. Извличане на идентификатори
        stripe_session_id = session_data.get("id")
        metadata = session_data.get("metadata") or {}
        order_id_raw = metadata.get("order_id") or session_data.get(
            "client_reference_id"
        )
        attempt_id_raw = metadata.get("attempt_id")

        order_id: int | None = None
        if order_id_raw is not None:
            try:
                order_id = int(order_id_raw)
            except ValueError:
                pass

        attempt_id: int | None = None
        if attempt_id_raw is not None:
            try:
                attempt_id = int(attempt_id_raw)
            except ValueError:
                pass

        if order_id is None and stripe_session_id:
            temp_attempt = self._order_repository.get_stripe_session_by_stripe_id(
                stripe_session_id
            )
            if temp_attempt:
                order_id = temp_attempt.order_id
                if attempt_id is None:
                    attempt_id = temp_attempt.id

        if order_id is None or not stripe_session_id:
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
                if self._order_repository.is_stripe_event_processed(event_id):
                    return {"status": "already_processed", "event_id": event_id}
                raise
            return {"status": "ignored", "reason": "missing_identifiers"}

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

            attempt_record: StripeCheckoutSession | None = None
            if attempt_id is not None:
                attempt_record = (
                    self._order_repository.get_stripe_session_by_id_for_update(
                        attempt_id
                    )
                )
            elif stripe_session_id is not None:
                attempt_record = (
                    self._order_repository.get_stripe_session_by_stripe_id_for_update(
                        stripe_session_id
                    )
                )

            if attempt_record is None:
                attempt_record = self._order_repository.get_latest_stripe_session_by_order_id_for_update(
                    order.id
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

            # Ранен webhook: свързваме сесията, ако още не е записан stripe_session_id
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

            # Проверка за mode на опита
            if attempt_record.mode != settings.stripe_mode:
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                        order_id=order.id,
                    )
                )
                self._session.commit()
                return {"status": "mismatched", "reason": "attempt_mode_mismatch"}

            # 7. Обработка според типа на събитието
            if event_type == "checkout.session.completed":
                payment_status = session_data.get("payment_status")
                if payment_status == "paid":
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

                    attempt_record.status = "completed"

                    if order.status in ("cancelled", "expired"):
                        self._order_repository.record_stripe_event(
                            StripeProcessedEvent(
                                stripe_event_id=event_id,
                                event_type=event_type,
                                order_id=order.id,
                            )
                        )
                        self._session.commit()
                        return {
                            "status": "discrepancy",
                            "reason": "payment_on_cancelled_order",
                            "event_id": event_id,
                        }

                    self._transition_order_to_paid(order)
                    self._order_repository.record_stripe_event(
                        StripeProcessedEvent(
                            stripe_event_id=event_id,
                            event_type=event_type,
                            order_id=order.id,
                        )
                    )
                    self._session.commit()
                    return {"status": "success", "event_id": event_id}
                else:
                    if order.status == "pending_payment":
                        order.status = "payment_processing"
                        attempt_record.status = "processing"
                    elif order.status == "payment_processing":
                        attempt_record.status = "processing"

                    self._order_repository.record_stripe_event(
                        StripeProcessedEvent(
                            stripe_event_id=event_id,
                            event_type=event_type,
                            order_id=order.id,
                        )
                    )
                    self._session.commit()
                    return {"status": "processing", "event_id": event_id}

            elif event_type == "checkout.session.async_payment_succeeded":
                amount_total = session_data.get("amount_total")
                currency = (session_data.get("currency") or "").upper()
                if amount_total is not None and (
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

                if currency and (
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

                attempt_record.status = "completed"

                if order.status in ("cancelled", "expired"):
                    self._order_repository.record_stripe_event(
                        StripeProcessedEvent(
                            stripe_event_id=event_id,
                            event_type=event_type,
                            order_id=order.id,
                        )
                    )
                    self._session.commit()
                    return {
                        "status": "discrepancy",
                        "reason": "payment_on_cancelled_order",
                        "event_id": event_id,
                    }

                self._transition_order_to_paid(order)
                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                        order_id=order.id,
                    )
                )
                self._session.commit()
                return {"status": "success", "event_id": event_id}

            elif event_type == "checkout.session.async_payment_failed":
                attempt_record.status = "failed"
                current_time = datetime.datetime.now(datetime.UTC)
                is_expired = (
                    order.expires_at is not None and order.expires_at <= current_time
                )
                if is_expired and order.status not in ("cancelled", "expired", "paid"):
                    order.status = "expired"
                    self._restore_order_stock(order)
                elif not is_expired and order.status == "payment_processing":
                    order.status = "pending_payment"

                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                        order_id=order.id,
                    )
                )
                self._session.commit()
                return {"status": "failed", "event_id": event_id}

            elif event_type == "checkout.session.expired":
                attempt_record.status = "expired"
                current_time = datetime.datetime.now(datetime.UTC)
                is_expired = (
                    order.expires_at is not None and order.expires_at <= current_time
                )
                if is_expired and order.status not in ("cancelled", "expired", "paid"):
                    order.status = "expired"
                    self._restore_order_stock(order)

                self._order_repository.record_stripe_event(
                    StripeProcessedEvent(
                        stripe_event_id=event_id,
                        event_type=event_type,
                        order_id=order.id,
                    )
                )
                self._session.commit()
                return {"status": "expired", "event_id": event_id}

            self._order_repository.record_stripe_event(
                StripeProcessedEvent(
                    stripe_event_id=event_id,
                    event_type=event_type,
                    order_id=order.id,
                )
            )
            self._session.commit()
            return {"status": "ignored", "event_id": event_id}

        except IntegrityError:
            self._session.rollback()
            if self._order_repository.is_stripe_event_processed(event_id):
                return {"status": "already_processed", "event_id": event_id}
            raise
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

        if order.status == "payment_processing":
            raise OrderStateConflict(
                current_status="payment_processing",
                message="Cannot cancel order while payment is processing",
            )

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

        # В. Сесията е в процес на асинхронно плащане -> отказът е забранен
        if latest_session.status == "processing":
            self._session.commit()
            raise OrderStateConflict(
                current_status="processing",
                message="Cannot cancel order while payment is processing",
            )

        # Г. Сесията вече е изтекла или неуспешна
        if latest_session.status in ("expired", "failed"):
            order.status = "cancelled"
            self._restore_order_stock(order)
            self._session.commit()
            self._session.refresh(order)
            _ = order.items
            return order

        # Д. initiated без Stripe ID: резултатът още не е известен!
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

        # Е. Активна или започнала прекратяване сесия с наличен Stripe ID
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

        # Повторно заключваме поръчката и опита в ред: поръчка -> опит -> стоки
        order = self._order_repository.get_by_id_and_user_id_for_update(
            order_id=order_id,
            user_id=user_id,
        )
        s_rec = self._order_repository.get_stripe_session_by_id_for_update(
            latest_session.id
        )

        if order.status == "paid":
            if s_rec:
                s_rec.status = "completed"
            self._session.commit()
            raise OrderAlreadyPaid("Payment completed before cancellation could finish")

        if order.status == "cancelled":
            if s_rec and s_rec.status != "expired":
                s_rec.status = "expired"
            self._session.commit()
            return order

        order.status = "cancelled"
        if s_rec:
            s_rec.status = "expired"
        self._restore_order_stock(order)
        self._session.commit()
        self._session.refresh(order)
        _ = order.items
        return order

    def _reconcile_single_order(
        self, order_id: int, current_time: datetime.datetime
    ) -> str:
        """Сверява единична поръчка според нейния статус, срок и състояние в Stripe."""
        # Фаза 1: Проверка на поръчката и последния опит в DB
        try:
            order = self._order_repository.get_by_id_for_update(order_id)
            if order is None:
                self._session.commit()
                return "skipped"

            if order.status not in ("pending_payment", "payment_processing"):
                self._session.commit()
                return "skipped"

            is_time_expired = (
                order.expires_at is not None and order.expires_at <= current_time
            )

            latest_session = (
                self._order_repository.get_latest_stripe_session_by_order_id_for_update(
                    order.id
                )
            )

            # Случай 1: Никога не е правен опит за Stripe плащане
            if latest_session is None:
                if is_time_expired:
                    order.status = "expired"
                    self._restore_order_stock(order)
                    self._session.commit()
                    return "expired"
                self._session.commit()
                return "deferred"

            # Случай 2: Опитът вече е завършен (платен)
            if latest_session.status == "completed":
                self._transition_order_to_paid(order)
                self._session.commit()
                return "paid"

            # Случай 3: Опитът вече е прекратен или неуспешен
            if latest_session.status in ("expired", "failed"):
                if is_time_expired:
                    order.status = "expired"
                    self._restore_order_stock(order)
                    self._session.commit()
                    return "expired"
                self._session.commit()
                return "deferred"

            # Случай 4: Опитът е initiated без Stripe ID (неизвестен резултат)
            if (
                latest_session.status == "initiated"
                and not latest_session.stripe_session_id
            ):
                if is_time_expired:
                    order.status = "expired"
                    latest_session.status = "expired"
                    self._restore_order_stock(order)
                    self._session.commit()
                    return "expired"
                self._session.commit()
                return "deferred"

            # Случай 5: Има stripe_session_id (open, processing, cancelling)
            stripe_session_id = latest_session.stripe_session_id
            attempt_id = latest_session.id
            session_status = latest_session.status

            # Освобождаваме DB заключването преди мрежовата заявка към Stripe
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        # Фаза 2: Мрежово извличане на актуалното състояние от Stripe
        try:
            stripe_session = self._stripe_client.retrieve_checkout_session(
                stripe_session_id
            )
        except Exception:  # noqa: BLE001
            # При недостъпен Stripe запазваме резерва
            return "deferred"

        stripe_payment_status = getattr(stripe_session, "payment_status", None)
        stripe_state = getattr(stripe_session, "status", None)

        # Фаза 3: Прилагане на резултата според отговора на Stripe
        try:
            # 3.1: Stripe показва успешно плащане
            if stripe_payment_status == "paid":
                order = self._order_repository.get_by_id_for_update(order_id)
                s_rec = self._order_repository.get_stripe_session_by_id_for_update(
                    attempt_id
                )
                if order:
                    self._transition_order_to_paid(order)
                if s_rec:
                    s_rec.status = "completed"
                self._session.commit()
                return "paid"

            # 3.2: Не е изтекъл срокът на поръчката: оставяме резерва
            if not is_time_expired:
                if stripe_payment_status != "paid" and stripe_state == "complete":
                    order = self._order_repository.get_by_id_for_update(order_id)
                    s_rec = self._order_repository.get_stripe_session_by_id_for_update(
                        attempt_id
                    )
                    if order and order.status == "pending_payment":
                        order.status = "payment_processing"
                    if s_rec:
                        s_rec.status = "processing"
                    self._session.commit()
                return "deferred"

            # 3.3: Срокът е изтекъл
            # Ако плащането се обработва асинхронно: запазваме резерва
            if session_status == "processing" or (
                stripe_state == "complete" and stripe_payment_status != "paid"
            ):
                return "deferred"

            # Ако сесията все още е open в Stripe: първо я прекратяваме в Stripe!
            if stripe_state == "open":
                try:
                    self._stripe_client.expire_checkout_session(stripe_session_id)
                except Exception:  # noqa: BLE001
                    # При срив на прекратяването запазваме резерва
                    return "deferred"

            # Сесията е потвърдено прекратена в Stripe -> освобождаваме резерва
            order = self._order_repository.get_by_id_for_update(order_id)
            s_rec = self._order_repository.get_stripe_session_by_id_for_update(
                attempt_id
            )
            if order and order.status not in ("cancelled", "expired", "paid"):
                order.status = "expired"
                if s_rec:
                    s_rec.status = "expired"
                self._restore_order_stock(order)
                self._session.commit()
                return "expired"

            self._session.commit()
            return "deferred"
        except Exception:
            self._session.rollback()
            raise

    def reconcile_unresolved_orders(
        self, now: datetime.datetime | None = None
    ) -> dict[str, int]:
        """Сверява всички неразрешени поръчки (pending_payment, payment_processing)."""
        current_time = now or datetime.datetime.now(datetime.UTC)
        order_ids = self._order_repository.list_unresolved_order_ids()

        stats = {
            "checked": len(order_ids),
            "expired": 0,
            "paid": 0,
            "deferred": 0,
            "skipped": 0,
        }

        for order_id in order_ids:
            try:
                res = self._reconcile_single_order(order_id, current_time)
                if res in stats:
                    stats[res] += 1
            except Exception:  # noqa: BLE001
                stats["deferred"] += 1

        return stats
