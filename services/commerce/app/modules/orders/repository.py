import datetime
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, selectinload

from app.modules.orders.models import (
    Order,
    OrderInboxEvent,
    OrderItem,
    OrderOutboxEvent,
    OrderPaymentDiscrepancy,
    PaymentAttempt,
    Shipment,
    StripeCheckoutSession,
    StripeProcessedEvent,
)


class OrderRepository:
    """Хранилище за достъп до данни за поръчки и техни позиции."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, order: Order) -> Order:
        """Добавя поръчка в сесията без commit."""
        self._session.add(order)
        return order

    def add_item(self, item: OrderItem) -> OrderItem:
        """Добавя позиция на поръчка в сесията без commit."""
        self._session.add(item)
        return item

    def get_by_user_and_idempotency_key(
        self,
        user_id: int,
        idempotency_key: str,
    ) -> Order | None:
        """Търси поръчка по user_id и idempotency_key с предварително заредени позиции и пратки."""
        stmt = (
            select(Order)
            .where(
                Order.user_id == user_id,
                Order.idempotency_key == idempotency_key,
            )
            .options(selectinload(Order.items), selectinload(Order.shipments))
        )
        return self._session.scalars(stmt).first()

    def get_by_id_and_user_id(
        self,
        order_id: int,
        user_id: int,
    ) -> Order | None:
        """Извлича поръчка по id и user_id с предварително заредени позиции и пратки."""
        stmt = (
            select(Order)
            .where(
                Order.id == order_id,
                Order.user_id == user_id,
            )
            .options(
                selectinload(Order.items),
                selectinload(Order.shipments),
                selectinload(Order.payment_discrepancies),
            )
        )
        return self._session.scalars(stmt).first()

    def get_by_id_and_user_id_for_update(
        self,
        order_id: int,
        user_id: int,
    ) -> Order | None:
        """Извлича и заключва ред от поръчката по id и user_id с FOR UPDATE."""
        stmt = (
            select(Order)
            .where(
                Order.id == order_id,
                Order.user_id == user_id,
            )
            .options(
                selectinload(Order.items),
                selectinload(Order.shipments),
                selectinload(Order.payment_discrepancies),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def get_by_id_for_update(self, order_id: int) -> Order | None:
        """Извлича и заключва ред от поръчката само по id с FOR UPDATE."""
        stmt = (
            select(Order)
            .where(Order.id == order_id)
            .options(
                selectinload(Order.items),
                selectinload(Order.shipments),
                selectinload(Order.payment_discrepancies),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def add_payment_attempt(self, attempt: PaymentAttempt) -> PaymentAttempt:
        """Добавя опит за плащане в сесията без commit."""
        self._session.add(attempt)
        return attempt

    def get_payment_attempt_by_order_and_key(
        self,
        order_id: int,
        idempotency_key: str,
    ) -> PaymentAttempt | None:
        """Извлича опит за плащане по order_id и idempotency_key."""
        stmt = select(PaymentAttempt).where(
            PaymentAttempt.order_id == order_id,
            PaymentAttempt.idempotency_key == idempotency_key,
        )
        return self._session.scalars(stmt).first()

    def add_stripe_session(
        self, session_record: StripeCheckoutSession
    ) -> StripeCheckoutSession:
        """Добавя запис за Stripe Checkout сесия в сесията без commit."""
        self._session.add(session_record)
        return session_record

    def get_open_or_initiated_stripe_session(
        self, order_id: int
    ) -> StripeCheckoutSession | None:
        """Извлича последната отворена или инициирана Stripe сесия за поръчка."""
        stmt = (
            select(StripeCheckoutSession)
            .where(
                StripeCheckoutSession.order_id == order_id,
                StripeCheckoutSession.status.in_(["open", "initiated"]),
            )
            .order_by(StripeCheckoutSession.id.desc())
        )
        return self._session.scalars(stmt).first()

    def get_latest_stripe_session_by_order_id_for_update(
        self, order_id: int
    ) -> StripeCheckoutSession | None:
        """Извлича последната Stripe сесия за поръчка със заключване FOR UPDATE."""
        stmt = (
            select(StripeCheckoutSession)
            .where(StripeCheckoutSession.order_id == order_id)
            .order_by(StripeCheckoutSession.id.desc())
            .with_for_update()
        )
        return self._session.scalars(stmt).first()

    def get_stripe_session_by_id(
        self, session_db_id: int
    ) -> StripeCheckoutSession | None:
        """Извлича Stripe сесия по нейния първичен ключ в БД."""
        return self._session.get(StripeCheckoutSession, session_db_id)

    def get_stripe_session_by_id_for_update(
        self, session_db_id: int
    ) -> StripeCheckoutSession | None:
        """Извлича Stripe сесия по ID със заключване FOR UPDATE."""
        stmt = (
            select(StripeCheckoutSession)
            .where(StripeCheckoutSession.id == session_db_id)
            .with_for_update()
        )
        return self._session.scalars(stmt).first()

    def get_stripe_session_by_stripe_id(
        self, stripe_session_id: str
    ) -> StripeCheckoutSession | None:
        """Извлича запис за сесия по нейния публичен Stripe session ID."""
        stmt = select(StripeCheckoutSession).where(
            StripeCheckoutSession.stripe_session_id == stripe_session_id
        )
        return self._session.scalars(stmt).first()

    def get_stripe_session_by_stripe_id_for_update(
        self, stripe_session_id: str
    ) -> StripeCheckoutSession | None:
        """Извлича запис за сесия по Stripe session ID със заключване FOR UPDATE."""
        stmt = (
            select(StripeCheckoutSession)
            .where(StripeCheckoutSession.stripe_session_id == stripe_session_id)
            .with_for_update()
        )
        return self._session.scalars(stmt).first()

    def is_stripe_event_processed(self, stripe_event_id: str) -> bool:
        """Проверява дали дадено webhook събитие вече е записано и обработено."""
        stmt = select(
            select(StripeProcessedEvent)
            .where(StripeProcessedEvent.stripe_event_id == stripe_event_id)
            .exists()
        )
        return bool(self._session.scalar(stmt))

    def list_unresolved_order_ids(self) -> Sequence[int]:
        """Извлича ID-тата на поръчки в pending_payment или payment_processing за сверка."""
        stmt = (
            select(Order.id)
            .where(Order.status.in_(["pending_payment", "payment_processing"]))
            .order_by(Order.id.asc())
        )
        return self._session.scalars(stmt).all()

    def record_stripe_event(
        self, event_record: StripeProcessedEvent
    ) -> StripeProcessedEvent:
        """Записва обработено Stripe webhook събитие за осигуряване на идемпотентност."""
        self._session.add(event_record)
        return event_record

    def list_by_user_id(
        self,
        user_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[Sequence[Order], int]:
        """Извлича пагиниран списък с поръчки за потребител и общия им брой.

        Подредбата е строго created_at DESC, id DESC.
        """
        count_stmt = (
            select(func.count()).select_from(Order).where(Order.user_id == user_id)
        )
        total = self._session.scalar(count_stmt) or 0

        stmt = (
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit)
            .offset(offset)
        )
        orders = self._session.scalars(stmt).all()
        return orders, total

    def record_outbox_event(self, event: OrderOutboxEvent) -> OrderOutboxEvent:
        """Записва събитие в outbox таблицата в същата транзакция."""
        self._session.add(event)
        return event

    def record_inbox_event_idempotent(
        self,
        *,
        event_id: str,
        event_type: str,
        event_version: int,
        payment_attempt_id: int | None,
        order_id: int,
        outcome: str,
        payload: dict[str, Any],
        occurred_at: datetime.datetime,
    ) -> bool:
        """Записва прието платежно събитие в inbox таблицата идемпотентно."""
        stmt = (
            pg_insert(OrderInboxEvent)
            .values(
                event_id=event_id,
                event_type=event_type,
                event_version=event_version,
                payment_attempt_id=payment_attempt_id,
                order_id=order_id,
                outcome=outcome,
                payload=payload,
                occurred_at=occurred_at,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(OrderInboxEvent.event_id)
        )
        return self._session.execute(stmt).scalar_one_or_none() is not None

    def is_inbox_event_processed(self, event_id: str) -> bool:
        stmt = select(
            select(OrderInboxEvent).where(OrderInboxEvent.event_id == event_id).exists()
        )
        return bool(self._session.scalar(stmt))

    def record_payment_discrepancy_idempotent(
        self,
        *,
        order_id: int,
        payment_attempt_id: int | None,
        event_id: str | None,
        reason: str,
        amount_minor: int,
        currency: str,
        stripe_session_id: str | None,
        stripe_event_id: str | None,
        details: dict[str, Any],
    ) -> int | None:
        """Записва видимо разхождение за плащане върху вече отменена/изтекла поръчка."""
        stmt = (
            pg_insert(OrderPaymentDiscrepancy)
            .values(
                order_id=order_id,
                payment_attempt_id=payment_attempt_id,
                event_id=event_id,
                reason=reason,
                amount_minor=amount_minor,
                currency=currency,
                stripe_session_id=stripe_session_id,
                stripe_event_id=stripe_event_id,
                status="open",
                details=details,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(OrderPaymentDiscrepancy.id)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_expired_reserve_order_ids(
        self, current_time: datetime.datetime
    ) -> Sequence[int]:
        """Извлича поръчки в pending_payment/payment_processing с изтекъл срок за проверка с Payments."""
        stmt = (
            select(Order.id)
            .where(
                Order.status.in_(["pending_payment", "payment_processing"]),
                Order.expires_at.is_not(None),
                Order.expires_at <= current_time,
            )
            .order_by(Order.id.asc())
        )
        return self._session.scalars(stmt).all()

    def get_outbox_event(
        self, order_id: int, event_type: str = "order.paid"
    ) -> OrderOutboxEvent | None:
        """Извлича outbox събитие за поръчка по тип събитие."""
        stmt = select(OrderOutboxEvent).where(
            OrderOutboxEvent.order_id == order_id,
            OrderOutboxEvent.event_type == event_type,
        )
        return self._session.scalars(stmt).first()

    def list_pending_outbox_events(self, limit: int = 50) -> Sequence[OrderOutboxEvent]:
        """Извлича чакащи изпращане outbox събития (status = 'pending')."""
        stmt = (
            select(OrderOutboxEvent)
            .where(OrderOutboxEvent.status == "pending")
            .order_by(OrderOutboxEvent.created_at.asc(), OrderOutboxEvent.id.asc())
            .limit(limit)
        )
        return self._session.scalars(stmt).all()

    def get_outbox_event_by_id_for_update(
        self, event_id: int
    ) -> OrderOutboxEvent | None:
        """Извлича outbox събитие по ID със заключване FOR UPDATE."""
        stmt = (
            select(OrderOutboxEvent)
            .where(OrderOutboxEvent.id == event_id)
            .with_for_update()
        )
        return self._session.scalars(stmt).first()

    def add_shipment(self, shipment: Shipment) -> Shipment:
        """Добавя пратка в сесията без commit."""
        self._session.add(shipment)
        return shipment

    def get_shipment_by_id(self, shipment_id: int) -> Shipment | None:
        """Извлича пратка по нейното ID с заредена свързана поръчка и нейните позиции."""
        stmt = (
            select(Shipment)
            .where(Shipment.id == shipment_id)
            .options(selectinload(Shipment.order).selectinload(Order.items))
        )
        return self._session.scalars(stmt).first()

    def get_shipment_by_id_for_update(self, shipment_id: int) -> Shipment | None:
        """Извлича и заключва пратка по нейното ID с FOR UPDATE."""
        stmt = (
            select(Shipment)
            .where(Shipment.id == shipment_id)
            .options(selectinload(Shipment.order).selectinload(Order.items))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def list_shipments_by_order_id(self, order_id: int) -> Sequence[Shipment]:
        """Извлича всички пратки към поръчка, сортирани по ID възходящо."""
        stmt = (
            select(Shipment)
            .where(Shipment.order_id == order_id)
            .order_by(Shipment.id.asc())
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).all()

    def list_shipments_by_seller(
        self,
        seller_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[Sequence[Shipment], int]:
        """Извлича пагиниран списък с пратки за конкретен продавач и общия им брой."""
        count_stmt = (
            select(func.count())
            .select_from(Shipment)
            .where(Shipment.seller_id == seller_id)
        )
        total = self._session.scalar(count_stmt) or 0

        stmt = (
            select(Shipment)
            .where(Shipment.seller_id == seller_id)
            .options(selectinload(Shipment.order).selectinload(Order.items))
            .order_by(Shipment.created_at.desc(), Shipment.id.desc())
            .limit(limit)
            .offset(offset)
        )
        shipments = self._session.scalars(stmt).all()
        return shipments, total

    def list_order_items_by_order_and_seller(
        self,
        order_id: int,
        seller_id: int,
    ) -> Sequence[OrderItem]:
        """Извлича позициите от дадена поръчка, които принадлежат на конкретен продавач."""
        stmt = (
            select(OrderItem)
            .where(
                OrderItem.order_id == order_id,
                OrderItem.seller_id == seller_id,
            )
            .order_by(OrderItem.product_id.asc())
        )
        return self._session.scalars(stmt).all()

    def get_outbox_event_by_shipment_id(
        self,
        shipment_id: int,
        event_type: str = "shipment.shipped",
    ) -> OrderOutboxEvent | None:
        """Извлича outbox събитие по shipment_id и event_type."""
        stmt = select(OrderOutboxEvent).where(
            OrderOutboxEvent.shipment_id == shipment_id,
            OrderOutboxEvent.event_type == event_type,
        )
        return self._session.scalars(stmt).first()
