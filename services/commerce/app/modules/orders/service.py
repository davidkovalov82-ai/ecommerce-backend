import datetime
import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.accounts.repository import UserRepository
from app.modules.accounts.users_client import (
    UsersClient,
    UsersServiceUnavailableError,
)
from app.modules.cart.repository import CartRepository
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.models import (
    Order,
    OrderItem,
    OrderOutboxEvent,
    Shipment,
)
from app.modules.orders.payment_contracts import ValidatedPaymentEvent
from app.modules.orders.payments_client import (
    HttpPaymentsClient,
    PaymentsClient,
    RemoteOrderAlreadyPaid,
    RemoteOrderAmountInvalid,
    RemotePaymentConflict,
)
from app.modules.orders.repository import OrderRepository
from app.modules.orders.schemas import ShippingAddress

logger = logging.getLogger(__name__)


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


class IdempotencyPayloadConflict(Exception):
    """Сигнализира за повторно използване на същия idempotency key с различен адрес или данни."""


class ShipmentNotFound(Exception):
    """Сигнализира, че пратката не съществува или принадлежи на друг продавач."""


class OrderNotPaid(Exception):
    """Сигнализира за опит за изпращане на неплатена поръчка."""


class MissingShippingData(Exception):
    """Сигнализира за опит за изпращане на поръчка без валиден адрес за доставка."""


class InvalidOrderAmount(Exception):
    """Сигнализира за сума на поръчката извън допустимите граници за Stripe."""


class OrderService:
    """Услуга за управление на жизнения цикъл на поръчките."""

    def __init__(
        self,
        order_repository: OrderRepository,
        cart_repository: CartRepository,
        product_repository: ProductRepository,
        user_repository: UserRepository | None = None,
        session: Session | None = None,
        payments_client: PaymentsClient | None = None,
        users_client: UsersClient | None = None,
    ) -> None:
        self._order_repository = order_repository
        self._cart_repository = cart_repository
        self._product_repository = product_repository
        self._user_repository = user_repository or (
            UserRepository(session) if session is not None else None
        )
        self._session = session
        self._payments_client = payments_client or HttpPaymentsClient()
        self._users_client = users_client or UsersClient()

    def create_order(
        self,
        user_id: int,
        idempotency_key: str,
        shipping_address: ShippingAddress,
        customer_email: str | None = None,
    ) -> Order:
        """Сценарий: оформяне на цялата текуща количка в нова поръчка с адрес и пратки.

        Транзакционен протокол:
        1. Заключване на потребителя с FOR NO KEY UPDATE за пълна сериализация.
        2. Проверка за съществуваща поръчка със същия ключ (идемпотентност).
           - При различен адрес -> хвърля IdempotencyPayloadConflict (409).
           - При съвпадащ адрес -> връща съществуващата поръчка без странични ефекти.
        3. Проверка за непразна количка.
        4. Заключване на засегнатите продукти във възходящ ред по ID с FOR UPDATE.
        5. Проверка на актуална активност и наличност, както и статус на продавача.
        6. Създаване на Order с копие на адреса и начален fulfillment_status='unshipped'.
        7. Създаване на Shipment за всеки уникален продавач (order_id, seller_id).
        8. Създаване на исторически OrderItem позиции с фиксиран seller_id.
        9. Намаляване на складовите наличности.
        10. Изчистване на потребителската количка.
        11. Commit на транзакцията.
        """
        try:
            # 1. Единна DB блокировка за текущия потребител по user_id
            if self._user_repository is not None:
                self._user_repository.lock_user(user_id)
            elif self._session is not None:
                self._session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": user_id},
                )

            # 2. Проверка за съществуваща поръчка със същия ключ (идемпотентност)
            existing_order = self._order_repository.get_by_user_and_idempotency_key(
                user_id=user_id,
                idempotency_key=idempotency_key,
            )
            if existing_order is not None:
                # Проверка дали адресът съвпада
                if (
                    existing_order.shipping_recipient_name
                    != shipping_address.recipient_name
                    or existing_order.shipping_phone != shipping_address.phone
                    or existing_order.shipping_country_code
                    != shipping_address.country_code
                    or existing_order.shipping_city != shipping_address.city
                    or existing_order.shipping_postal_code
                    != shipping_address.postal_code
                    or existing_order.shipping_address_line1
                    != shipping_address.address_line1
                    or existing_order.shipping_address_line2
                    != shipping_address.address_line2
                ):
                    raise IdempotencyPayloadConflict(
                        "Idempotency key was already used with a different shipping address"
                    )
                return existing_order

            # 3. Прочитане на текущата количка
            cart_items = self._cart_repository.list_by_user_id(user_id)
            if not cart_items:
                raise CartEmpty("Shopping cart is empty")

            # 4. Заключване на продуктите във възходящ ред по id
            product_ids = [item.product_id for item in cart_items]
            locked_products = self._product_repository.get_by_ids_for_update(
                product_ids
            )
            products_by_id = {p.id: p for p in locked_products}

            # 5. Проверка на активност, наличност и статус на продавачите през Users
            if self._users_client is None:
                raise UsersServiceUnavailableError(
                    "Users service client is not configured"
                )

            unique_seller_ids = sorted(
                {p.seller_id for p in locked_products if p.seller_id is not None}
            )
            sellers_by_id: dict[int, object] = {}

            for sid in unique_seller_ids:
                remote_seller = self._users_client.get_user_status(sid)
                if remote_seller is None or remote_seller.status != "active":
                    offending_item = next(
                        item
                        for item in cart_items
                        if products_by_id.get(item.product_id)
                        and products_by_id[item.product_id].seller_id == sid
                    )
                    raise CartProductUnavailable(offending_item.product_id)
                sellers_by_id[sid] = remote_seller

            order_items_data: list[tuple[int, str, int, int, int, int]] = []
            total_minor = 0

            for item in cart_items:
                product = products_by_id.get(item.product_id)
                if product is None or not product.active:
                    raise CartProductUnavailable(item.product_id)

                if product.seller_id is None or product.seller_id not in sellers_by_id:
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
                        product.seller_id,
                    )
                )

            # 6. Създаване на поръчката с адрес за доставка
            now = datetime.datetime.now(datetime.UTC)
            expires_at = now + datetime.timedelta(
                minutes=settings.order_reserve_timeout_minutes
            )
            order = Order(
                user_id=user_id,
                customer_email=customer_email,
                status="pending_payment",
                fulfillment_status="unshipped",
                currency="EUR",
                total_minor=total_minor,
                idempotency_key=idempotency_key,
                shipping_recipient_name=shipping_address.recipient_name,
                shipping_phone=shipping_address.phone,
                shipping_country_code=shipping_address.country_code,
                shipping_city=shipping_address.city,
                shipping_postal_code=shipping_address.postal_code,
                shipping_address_line1=shipping_address.address_line1,
                shipping_address_line2=shipping_address.address_line2,
                expires_at=expires_at,
            )
            self._order_repository.add(order)
            self._session.flush()
            order.payment_attempt_seq = 1
            order.active_attempt_key = f"order_{order.id}_attempt_1"

            # 7. Създаване на отделна пратка (Shipment) за всеки уникален продавач
            unique_seller_ids = sorted({p.seller_id for p in locked_products})
            for s_id in unique_seller_ids:
                shipment = Shipment(
                    order_id=order.id,
                    seller_id=s_id,
                    status="unshipped",
                )
                self._order_repository.add_shipment(shipment)

            # 8. Създаване на OrderItem позиции с фиксиран seller_id
            for (
                p_id,
                p_title,
                qty,
                u_price,
                subtot,
                s_id,
            ) in order_items_data:
                order_item = OrderItem(
                    order_id=order.id,
                    product_id=p_id,
                    seller_id=s_id,
                    product_title=p_title,
                    quantity=qty,
                    unit_price_minor=u_price,
                    subtotal_minor=subtot,
                )
                self._order_repository.add_item(order_item)

                # 9. Намаляване на наличността
                product = products_by_id[p_id]
                product.stock -= qty

            # 10. Изчистване на поръчаната количка
            self._cart_repository.clear_by_user_id(user_id)

            # 11. Записване на транзакцията
            self._session.flush()
            self._session.commit()
            self._session.refresh(order)
            _ = order.items
            _ = order.shipments
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

    def list_seller_shipments(
        self,
        seller_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[Sequence[Shipment], int]:
        """Сценарий: пагиниран списък със собствени пратки на текущия продавач."""
        return self._order_repository.list_shipments_by_seller(
            seller_id=seller_id,
            limit=limit,
            offset=offset,
        )

    def get_seller_shipment(
        self,
        shipment_id: int,
        seller_id: int,
    ) -> Shipment:
        """Сценарий: извличане на детайли за конкретна собствена пратка на продавача."""
        shipment = self._order_repository.get_shipment_by_id(shipment_id)
        if shipment is None or shipment.seller_id != seller_id:
            raise ShipmentNotFound(f"Shipment {shipment_id} not found")
        return shipment

    def ship_seller_shipment(
        self,
        shipment_id: int,
        seller_id: int,
    ) -> Shipment:
        """Сценарий: продавач отбелязва пратката си като изпратена.

        Транзакционен протокол:
        1. Заключване на пратката с FOR UPDATE.
        2. Проверка за права (чужда или липсваща пратка дава 404).
        3. Заключване на поръчката с FOR UPDATE.
        4. Проверка на потвърдения статус на поръчката (status == 'paid', иначе 409).
        5. Проверка за наличен адрес (иначе 409).
        6. Ако пратката вече е 'shipped': връща текущото състояние без нов outbox запис (идемпотентност).
        7. Ако пратката е 'unshipped':
           - Обновяване на пратката (status = 'shipped', shipped_at = now).
           - Преизчисляване на fulfillment_status на поръчката ('shipped' или 'partially_shipped').
           - Запис на outbox събитие shipment.shipped в същата транзакция.
        8. Commit.
        """
        try:
            shipment = self._order_repository.get_shipment_by_id_for_update(shipment_id)
            if shipment is None or shipment.seller_id != seller_id:
                raise ShipmentNotFound(f"Shipment {shipment_id} not found")

            order = self._order_repository.get_by_id_for_update(shipment.order_id)
            if order is None:
                raise ShipmentNotFound(f"Order for shipment {shipment_id} not found")

            # Проверка дали поръчката е платена
            if order.status != "paid":
                raise OrderNotPaid(
                    f"Cannot ship order with payment status '{order.status}'"
                )

            # Проверка за наличен адрес за доставка (стари поръчки)
            if not order.shipping_recipient_name or not order.shipping_address_line1:
                raise MissingShippingData("Cannot ship order without shipping address")

            # 6. Повтор на вече изпратена пратка -> идемпотентно връща състоянието
            if shipment.status == "shipped":
                return shipment

            # 7. Изпращане на пратката
            now = datetime.datetime.now(datetime.UTC)
            shipment.status = "shipped"
            shipment.shipped_at = now
            self._session.flush()

            # Преизчисляване на fulfillment_status на поръчката
            all_shipments = self._order_repository.list_shipments_by_order_id(order.id)
            shipped_count = sum(
                1 for s in all_shipments if s.id == shipment.id or s.status == "shipped"
            )
            if shipped_count >= len(all_shipments):
                order.fulfillment_status = "shipped"
            else:
                order.fulfillment_status = "partially_shipped"

            # Намиране на контактите на купувача за известието от записания email в поръчката
            customer_email = order.customer_email
            if not customer_email:
                from app.modules.accounts.models import User

                user_row = self._session.query(User).filter_by(id=order.user_id).first()
                customer_email = user_row.email if (user_row and user_row.email) else ""
            customer_phone = (order.shipping_phone or "").strip() or None

            # Позиции само за този продавач
            seller_items = [
                {
                    "product_id": it.product_id,
                    "product_title": it.product_title,
                    "quantity": it.quantity,
                }
                for it in shipment.items
            ]

            # Запис на Outbox събитие в същата транзакция
            outbox_event = OrderOutboxEvent(
                event_id=str(uuid.uuid4()),
                event_version=1,
                occurred_at=now,
                order_id=order.id,
                shipment_id=shipment.id,
                event_type="shipment.shipped",
                payload={
                    "customer_email": customer_email,
                    "customer_phone": customer_phone,
                    "order_id": order.id,
                    "shipment_id": shipment.id,
                    "seller_id": seller_id,
                    "items": seller_items,
                },
                status="pending",
            )
            self._order_repository.record_outbox_event(outbox_event)
            self._session.flush()
            self._session.commit()
            self._session.refresh(shipment)
            return shipment
        except (ShipmentNotFound, OrderNotPaid, MissingShippingData):
            self._session.rollback()
            raise
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

        customer_email = order.customer_email
        if not customer_email:
            from app.modules.accounts.models import User

            user_row = self._session.query(User).filter_by(id=order.user_id).first()
            customer_email = user_row.email if (user_row and user_row.email) else ""
        customer_phone = (order.shipping_phone or "").strip() or None
        now = datetime.datetime.now(datetime.UTC)

        payload = {
            "order_id": order.id,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "total_minor": order.total_minor,
            "currency": order.currency,
            "created_at": order.created_at.isoformat() if order.created_at else None,
        }
        event = OrderOutboxEvent(
            event_id=str(uuid.uuid4()),
            event_version=1,
            occurred_at=now,
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
        """Сценарий: създаване или извличане на активна Stripe Checkout сесия чрез Payments сервиса.

        Транзакционен протокол:
        1. Заключване на поръчката с FOR UPDATE.
        2. Проверка за собственост, съществуване, статус pending_payment и лимити на сумата.
        3. Проверка за изтекъл резерв: ако поръчката вече е просрочена, първо освобождаваме DB
           заключването и координираме прекратяване с Payments, едва след което освобождаваме резерва.
        4. Фиксиране на текущия attempt_key и commit Преди HTTP заявката към Payments.
        5. Извикване на Payments internal HTTP API без отворена DB транзакция.
        """
        expired_order_snapshot: datetime.datetime | None = None
        is_reserve_expired = False

        # Фаза 1: Проверка на поръчката и подготовка на параметрите под FOR UPDATE
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

            now = datetime.datetime.now(datetime.UTC)
            if order.expires_at and order.expires_at <= now:
                is_reserve_expired = True
                expired_order_snapshot = order.expires_at
                self._session.commit()
            else:
                if not order.active_attempt_key:
                    seq = max(1, int(order.payment_attempt_seq or 1))
                    order.payment_attempt_seq = seq
                    order.active_attempt_key = f"order_{order.id}_attempt_{seq}"

                attempt_key = order.current_attempt_key
                amount_minor = order.total_minor
                currency = order.currency
                order_expires_at = order.expires_at
                line_items = [
                    {
                        "name": item.product_title,
                        "unit_amount": item.unit_price_minor,
                        "quantity": item.quantity,
                        "currency": order.currency.lower(),
                    }
                    for item in order.items
                ]
                self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        # Просрочен резерв -> съгласувано прекратяване в Payments преди връщане на наличност
        if is_reserve_expired:
            try:
                self._payments_client.cancel_order_payment(
                    order_id=order_id,
                    reason="reserve_expired",
                    order_expires_at=expired_order_snapshot,
                )
            except RemoteOrderAlreadyPaid as exc:
                raise OrderStateConflict(
                    current_status="paid",
                    message="Cannot expire or pay order: payment already completed",
                ) from exc
            except RemotePaymentConflict as exc:
                raise OrderStateConflict(
                    current_status=exc.current_status,
                    message=str(exc),
                ) from exc

            try:
                order_rec = self._order_repository.get_by_id_for_update(order_id)
                if order_rec and order_rec.status not in (
                    "cancelled",
                    "expired",
                    "paid",
                ):
                    order_rec.status = "expired"
                    self._restore_order_stock(order_rec)
                self._session.commit()
            except Exception:
                self._session.rollback()
                raise

            raise OrderStateConflict(
                current_status="expired",
                message="Order reserve has expired",
            )

        # Фаза 2: Вътрешен HTTP разговор към Payments без отворена DB транзакция
        try:
            session_result = self._payments_client.create_checkout_session(
                order_id=order_id,
                attempt_key=attempt_key,
                amount_minor=amount_minor,
                currency=currency,
                line_items=line_items,
                order_expires_at=order_expires_at,
            )
        except RemoteOrderAmountInvalid as exc:
            raise InvalidOrderAmount(str(exc)) from exc
        except RemoteOrderAlreadyPaid as exc:
            raise OrderStateConflict(
                current_status="paid",
                message="Cannot create checkout session for paid order",
            ) from exc
        except RemotePaymentConflict as exc:
            raise OrderStateConflict(
                current_status=exc.current_status,
                message=str(exc),
            ) from exc

        return session_result.checkout_url, session_result.session_id

    def apply_payment_event(self, event: ValidatedPaymentEvent) -> str:
        """Прилага валидирано платежно събитие от Payments към състоянието на поръчката (идемпотентно)."""
        if self._order_repository.is_inbox_event_processed(event.event_id):
            return "already_processed"

        try:
            order = self._order_repository.get_by_id_for_update(event.order_id)
            if order is None:
                inserted = self._order_repository.record_inbox_event_idempotent(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    event_version=event.event_version,
                    payment_attempt_id=event.payment_attempt_id,
                    order_id=event.order_id,
                    outcome="order_not_found",
                    payload=event.payload,
                    occurred_at=event.occurred_at,
                )
                self._session.commit()
                return "order_not_found" if inserted else "already_processed"

            current_attempt_key = order.current_attempt_key
            outcome = "ignored"

            if event.event_type == "payment.succeeded":
                if (
                    event.amount_minor is None
                    or event.currency is None
                    or event.amount_minor != order.total_minor
                    or event.currency.upper() != order.currency.upper()
                ):
                    outcome = "mismatched"
                elif order.status in ("cancelled", "expired"):
                    reason = (
                        "payment_on_cancelled_order"
                        if order.status == "cancelled"
                        else "payment_on_expired_order"
                    )
                    self._order_repository.record_payment_discrepancy_idempotent(
                        order_id=order.id,
                        payment_attempt_id=event.payment_attempt_id,
                        event_id=event.event_id,
                        reason=reason,
                        amount_minor=event.amount_minor,
                        currency=event.currency.upper(),
                        stripe_session_id=event.stripe_session_id,
                        stripe_event_id=event.stripe_event_id,
                        details={
                            "order_status": order.status,
                            "attempt_key": event.attempt_key,
                            "occurred_at": event.occurred_at.isoformat(),
                        },
                    )
                    outcome = "discrepancy"
                elif order.status == "paid":
                    self._transition_order_to_paid(order)
                    outcome = "already_paid"
                else:
                    self._transition_order_to_paid(order)
                    outcome = "paid"

            elif event.event_type == "payment.processing":
                if event.attempt_key != current_attempt_key:
                    outcome = "ignored_stale_attempt"
                elif order.status == "pending_payment":
                    order.status = "payment_processing"
                    outcome = "processing"
                elif order.status == "payment_processing":
                    outcome = "processing"
                else:
                    outcome = "ignored"

            elif event.event_type in ("payment.failed", "payment.expired"):
                if event.attempt_key != current_attempt_key:
                    outcome = "ignored_stale_attempt"
                else:
                    now = datetime.datetime.now(datetime.UTC)
                    is_expired = (
                        order.expires_at is not None and order.expires_at <= now
                    )
                    if order.status in ("paid", "cancelled", "expired"):
                        outcome = "ignored"
                    elif is_expired:
                        order.status = "expired"
                        self._restore_order_stock(order)
                        outcome = "expired"
                    else:
                        if order.status == "payment_processing":
                            order.status = "pending_payment"
                        # Отваряме нов ключ за следващ опит след окончателно неуспех/изтичане
                        next_seq = max(1, int(order.payment_attempt_seq or 1)) + 1
                        order.payment_attempt_seq = next_seq
                        order.active_attempt_key = (
                            f"order_{order.id}_attempt_{next_seq}"
                        )
                        outcome = (
                            "failed"
                            if event.event_type == "payment.failed"
                            else "expired"
                        )

            elif event.event_type == "payment.cancelled":
                # Потвърдена отмяна на цялата поръчка от Payments (игнорира вече платени/финализирани)
                if order.status in ("paid", "cancelled", "expired"):
                    outcome = "ignored"
                else:
                    order.status = "cancelled"
                    self._restore_order_stock(order)
                    outcome = "cancelled"

            inserted = self._order_repository.record_inbox_event_idempotent(
                event_id=event.event_id,
                event_type=event.event_type,
                event_version=event.event_version,
                payment_attempt_id=event.payment_attempt_id,
                order_id=event.order_id,
                outcome=outcome,
                payload=event.payload,
                occurred_at=event.occurred_at,
            )
            if not inserted:
                self._session.rollback()
                return "already_processed"

            self._session.commit()
            return outcome
        except Exception:
            self._session.rollback()
            raise

    def cancel_order(self, order_id: int, user_id: int) -> Order:
        """Сценарий: съгласуван отказ от неплатена поръчка чрез Payments и връщане на наличностите."""
        # Фаза 1: Проверка на поръчката под FOR UPDATE и освобождаване на заключването преди HTTP
        order = self._order_repository.get_by_id_and_user_id_for_update(
            order_id=order_id,
            user_id=user_id,
        )
        if order is None:
            raise OrderNotFound(f"Order with id {order_id} not found")

        if order.status == "cancelled":
            _ = order.items
            return order

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

        expires_at_snapshot = order.expires_at
        self._session.commit()

        # Фаза 2: Прекратяване/проверка в Payments извън DB заключването
        try:
            self._payments_client.cancel_order_payment(
                order_id=order_id,
                reason="cancel",
                order_expires_at=expires_at_snapshot,
            )
        except RemoteOrderAlreadyPaid as exc:
            raise OrderAlreadyPaid("Cannot cancel paid order") from exc
        except RemotePaymentConflict as exc:
            raise OrderStateConflict(
                current_status=exc.current_status,
                message=str(exc),
            ) from exc

        # Фаза 3: Повторно заключване на поръчката и връщане на наличностите
        try:
            order = self._order_repository.get_by_id_and_user_id_for_update(
                order_id=order_id,
                user_id=user_id,
            )
            if order is None:
                raise OrderNotFound(f"Order with id {order_id} not found")

            if order.status == "paid":
                self._session.commit()
                raise OrderAlreadyPaid(
                    "Payment completed before cancellation could finish"
                )

            if order.status == "cancelled":
                _ = order.items
                return order

            if order.status == "payment_processing":
                self._session.commit()
                raise OrderStateConflict(
                    current_status="payment_processing",
                    message="Cannot cancel order while payment is processing",
                )

            if order.status != "pending_payment":
                self._session.commit()
                raise OrderStateConflict(
                    current_status=order.status,
                    message=f"Cannot cancel order with status '{order.status}'",
                )

            order.status = "cancelled"
            self._restore_order_stock(order)
            self._session.commit()
            self._session.refresh(order)
            _ = order.items
            return order
        except Exception:
            self._session.rollback()
            raise

    def _reconcile_single_expired_order(
        self, order_id: int, current_time: datetime.datetime
    ) -> str:
        """Сверява единична просрочена поръчка с Payments и освобождава резерва при потвърждение."""
        # Фаза 1: Проверка под FOR UPDATE и снимка на expires_at
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
            if not is_time_expired:
                self._session.commit()
                return "deferred"

            expires_at_snapshot = order.expires_at
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        # Фаза 2: Проверка/прекратяване в Payments без отворена DB транзакция
        try:
            self._payments_client.cancel_order_payment(
                order_id=order_id,
                reason="reserve_expired",
                order_expires_at=expires_at_snapshot,
            )
        except RemoteOrderAlreadyPaid:
            # Плащането вече е потвърдено в Payments -> пазим резерва за входящия payment.succeeded
            return "deferred"
        except RemotePaymentConflict:
            return "deferred"

        # Фаза 3: Потвърден отказ в Payments -> освобождаваме резерва в Commerce
        try:
            order = self._order_repository.get_by_id_for_update(order_id)
            if order is None:
                self._session.commit()
                return "skipped"

            if order.status in ("paid", "cancelled", "expired"):
                self._session.commit()
                return "skipped"

            if order.expires_at is None or order.expires_at > current_time:
                self._session.commit()
                return "deferred"

            order.status = "expired"
            self._restore_order_stock(order)
            self._session.commit()
            return "expired"
        except Exception:
            self._session.rollback()
            raise

    def reconcile_expired_orders(
        self, now: datetime.datetime | None = None
    ) -> dict[str, int]:
        """Сверява всички неплатени поръчки с изтекъл срок на резерва чрез Payments."""
        current_time = now or datetime.datetime.now(datetime.UTC)
        order_ids = self._order_repository.list_expired_reserve_order_ids(current_time)

        stats = {
            "checked": len(order_ids),
            "expired": 0,
            "paid": 0,
            "deferred": 0,
            "skipped": 0,
        }

        for order_id in order_ids:
            try:
                res = self._reconcile_single_expired_order(order_id, current_time)
                if res in stats:
                    stats[res] += 1
            except Exception:  # noqa: BLE001
                stats["deferred"] += 1

        return stats
