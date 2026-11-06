from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.modules.orders.models import (
    Order,
    OrderItem,
    PaymentAttempt,
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
        """Търси поръчка по user_id и idempotency_key с предварително заредени позиции."""
        stmt = (
            select(Order)
            .where(
                Order.user_id == user_id,
                Order.idempotency_key == idempotency_key,
            )
            .options(selectinload(Order.items))
        )
        return self._session.scalars(stmt).first()

    def get_by_id_and_user_id(
        self,
        order_id: int,
        user_id: int,
    ) -> Order | None:
        """Извлича поръчка по id и user_id с предварително заредени позиции."""
        stmt = (
            select(Order)
            .where(
                Order.id == order_id,
                Order.user_id == user_id,
            )
            .options(selectinload(Order.items))
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
            .options(selectinload(Order.items))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.scalars(stmt).first()

    def get_by_id_for_update(self, order_id: int) -> Order | None:
        """Извлича и заключва ред от поръчката само по id с FOR UPDATE (за webhooks)."""
        stmt = (
            select(Order)
            .where(Order.id == order_id)
            .options(selectinload(Order.items))
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
