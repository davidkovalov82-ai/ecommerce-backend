import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.modules.accounts.repository import UserRepository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.models import Product
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.dependencies import get_payments_client
from app.modules.orders.models import Order
from app.modules.orders.payments_client import (
    CancelPaymentResult,
    CheckoutSessionResult,
    PaymentGatewayError,
    PaymentsClient,
    PaymentsNotConfigured,
    RemoteOrderAlreadyPaid,
    RemotePaymentConflict,
)
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from tests.payments.helpers import (
    _create_order,
    _create_product,
    _create_user_and_token,
    make_payment_event,
)

client = TestClient(app)


def test_cancel_during_session_creation_does_not_prematurely_release_stock(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Ако в Payments се създава сесия, cancel връща 409 и НЕ освобождава stock."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "ConcurrentItem", price_minor=2000, stock=10)
    order = _create_order(db_session, user.id, product, quantity=3)  # stock=7

    mock_client = MagicMock(spec=PaymentsClient)
    mock_client.cancel_order_payment.side_effect = RemotePaymentConflict(
        current_status="initiated",
        message="Payment session creation is in progress",
    )
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    with session_factory() as s:
        assert s.get(Order, order.id).status == "pending_payment"  # type: ignore[union-attr]
        assert s.get(Product, product.id).stock == 7  # type: ignore[union-attr]


def test_cancel_order_payments_failure_does_not_restore_stock(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """При 502 от Payments поръчката остава активна и резервът НЕ се пипа."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "FailCancelItem", price_minor=2000, stock=10)
    order = _create_order(db_session, user.id, product, quantity=3)  # stock=7

    mock_client = MagicMock(spec=PaymentsClient)
    mock_client.cancel_order_payment.side_effect = PaymentGatewayError(
        "Payments unreachable"
    )
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 502
        assert resp.json()["detail"]["code"] == "PAYMENT_GATEWAY_ERROR"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    with session_factory() as s:
        assert s.get(Order, order.id).status == "pending_payment"  # type: ignore[union-attr]
        assert s.get(Product, product.id).stock == 7  # type: ignore[union-attr]


def test_cancel_order_payments_not_configured_returns_503(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Липсваща конфигурация за комуникация с Payments връща 503 и пази stock."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "NoKeyCancel", price_minor=2000, stock=10)
    order = _create_order(db_session, user.id, product, quantity=2)  # stock=8

    mock_client = MagicMock(spec=PaymentsClient)
    mock_client.cancel_order_payment.side_effect = PaymentsNotConfigured(
        "missing internal token"
    )
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        resp = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "PAYMENTS_NOT_CONFIGURED"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    with session_factory() as s:
        assert s.get(Order, order.id).status == "pending_payment"  # type: ignore[union-attr]
        assert s.get(Product, product.id).stock == 8  # type: ignore[union-attr]


def test_cancel_order_confirmed_by_payments_restores_stock(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """След потвърждение от Payments отмяната превежда поръчката в cancelled и връща stock."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "CancelOkItem", price_minor=2000, stock=10)
    order1 = _create_order(db_session, user.id, product, quantity=4)  # stock=6
    order2 = _create_order(db_session, user.id, product, quantity=1)  # stock=5

    mock_client = MagicMock(spec=PaymentsClient)
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        mock_client.cancel_order_payment.return_value = CancelPaymentResult(
            status="can_cancel", action="expired_stripe_session"
        )
        r1 = client.post(
            f"/orders/{order1.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "cancelled"

        mock_client.cancel_order_payment.return_value = CancelPaymentResult(
            status="already_expired", action="already_expired"
        )
        r2 = client.post(
            f"/orders/{order2.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "cancelled"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    with session_factory() as s:
        assert s.get(Product, product.id).stock == 10  # type: ignore[union-attr]


def test_cancel_order_race_condition_payment_wins(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Ако поръчката стане paid по време на cancel или Payments върне paid -> 409 и stock не се връща."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "RaceItem", price_minor=2000, stock=10)
    order1 = _create_order(db_session, user.id, product, quantity=2)  # stock=8
    order2 = _create_order(db_session, user.id, product, quantity=1)  # stock=7

    mock_client = MagicMock(spec=PaymentsClient)

    def mark_order_paid_during_http(**_kwargs):  # type: ignore[no-untyped-def]
        with session_factory() as s:
            o = s.get(Order, order1.id)
            assert o is not None
            o.status = "paid"
            s.commit()
        return CancelPaymentResult(status="can_cancel", action="expired")

    mock_client.cancel_order_payment.side_effect = mark_order_paid_during_http
    app.dependency_overrides[get_payments_client] = lambda: mock_client
    try:
        r1 = client.post(
            f"/orders/{order1.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 409
        assert r1.json()["detail"]["code"] == "ORDER_ALREADY_PAID"

        mock_client.cancel_order_payment.side_effect = RemoteOrderAlreadyPaid(
            "Payment already completed"
        )
        r2 = client.post(
            f"/orders/{order2.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 409
        assert r2.json()["detail"]["code"] == "ORDER_ALREADY_PAID"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    with session_factory() as s:
        assert s.get(Product, product.id).stock == 7  # type: ignore[union-attr]
        assert s.get(Order, order1.id).status == "paid"  # type: ignore[union-attr]
        assert s.get(Order, order2.id).status == "pending_payment"  # type: ignore[union-attr]


def test_cannot_cancel_or_create_checkout_in_payment_processing(
    db_session: Session,
) -> None:
    """Поръчка в payment_processing не може да бъде отказана или пренасочена към нов checkout."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "AsyncProd", price_minor=1500, stock=5)
    order = _create_order(db_session, user.id, product, status="payment_processing")

    r_cancel = client.post(
        f"/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_cancel.status_code == 409
    assert r_cancel.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"

    r_checkout = client.post(
        f"/orders/{order.id}/checkout-session",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_checkout.status_code == 409
    assert r_checkout.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"


def test_checkout_after_cancel_race_never_creates_stripe_session_or_duplicates_stock_restore(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Гонка checkout/cancel без предишен опит: завършен cancel блокира закъснял checkout и остатъкът се връща веднъж."""
    user, token = _create_user_and_token(db_session)
    product = _create_product(db_session, "RaceCancelItem", price_minor=2500, stock=10)
    order = _create_order(db_session, user.id, product, quantity=3)  # stock=7

    entered_checkout = threading.Event()
    allow_checkout_resume = threading.Event()
    lock = threading.Lock()
    closed_orders: dict[int, str] = {}
    stripe_sessions_created = 0

    class StatefulRacePaymentsClient:
        def create_checkout_session(
            self,
            *,
            order_id: int,
            attempt_key: str,
            amount_minor: int,
            currency: str,
            line_items: list[dict[str, object]],
            order_expires_at: object,
        ) -> CheckoutSessionResult:
            nonlocal stripe_sessions_created
            entered_checkout.set()
            assert allow_checkout_resume.wait(timeout=5.0)
            with lock:
                if order_id in closed_orders:
                    raise RemotePaymentConflict(
                        current_status=closed_orders[order_id],
                        message="Order already cancelled in Payments",
                    )
                stripe_sessions_created += 1
                return CheckoutSessionResult(
                    payment_attempt_id=1,
                    session_id=f"cs_race_{order_id}",
                    checkout_url=f"https://checkout.stripe.com/pay/cs_race_{order_id}",
                    status="open",
                )

        def cancel_order_payment(
            self,
            *,
            order_id: int,
            reason: str = "cancel",
            order_expires_at: object = None,
        ) -> CancelPaymentResult:
            with lock:
                closed_orders[order_id] = "cancelled"
                return CancelPaymentResult(status="can_cancel", action="no_attempt")

    app.dependency_overrides[get_payments_client] = StatefulRacePaymentsClient
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            checkout_future = pool.submit(
                lambda: client.post(
                    f"/orders/{order.id}/checkout-session",
                    headers={"Authorization": f"Bearer {token}"},
                )
            )
            assert entered_checkout.wait(timeout=5.0)

            r_cancel = client.post(
                f"/orders/{order.id}/cancel",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r_cancel.status_code == 200
            assert r_cancel.json()["status"] == "cancelled"

            allow_checkout_resume.set()
            r_checkout = checkout_future.result(timeout=5.0)
            assert r_checkout.status_code == 409
            assert r_checkout.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"
            assert "checkout_url" not in r_checkout.json()

            # Повторна отмяна е идемпотентна (200 cancelled) и не дублира възстановяването на stock
            r_repeat_cancel = client.post(
                f"/orders/{order.id}/cancel",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r_repeat_cancel.status_code == 200
            assert r_repeat_cancel.json()["status"] == "cancelled"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    assert stripe_sessions_created == 0
    with session_factory() as verify_session:
        refreshed_product = verify_session.get(Product, product.id)
        refreshed_order = verify_session.get(Order, order.id)
        assert refreshed_product is not None and refreshed_product.stock == 10
        assert refreshed_order is not None and refreshed_order.status == "cancelled"


def test_cancel_order_concurrent_payment_expired_and_retry_checkout_leaves_cancelled_and_single_restore(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Сценарий 3 (Commerce): отмяна в полет + payment.expired (attempt_2) + опит за нов checkout -> отмяната побеждава с 1 възстановяване."""
    user, token = _create_user_and_token(
        db_session, email="cancel_exp_checkout_race@example.com"
    )
    product = _create_product(
        db_session, "CancelExpiredRaceProd", price_minor=2000, stock=10
    )
    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=25)
    order = _create_order(
        db_session, user.id, product, quantity=2, expires_at=future
    )  # stock=8

    cancel_entered = threading.Event()
    allow_cancel_finish = threading.Event()
    lock = threading.Lock()
    cancelling_orders: set[int] = set()
    closed_orders: set[int] = set()
    stripe_sessions_created = 0
    seen_attempt_keys: list[str] = []

    class ClosureCoordinatingPaymentsClient:
        def create_checkout_session(
            self,
            *,
            order_id: int,
            attempt_key: str,
            amount_minor: int,
            currency: str,
            line_items: list[dict[str, object]],
            order_expires_at: object,
        ) -> CheckoutSessionResult:
            nonlocal stripe_sessions_created
            seen_attempt_keys.append(attempt_key)
            with lock:
                if order_id in cancelling_orders or order_id in closed_orders:
                    raise RemotePaymentConflict(
                        current_status="cancelled"
                        if order_id in closed_orders
                        else "cancelling",
                        message="Order cancellation is active/completed",
                    )
                stripe_sessions_created += 1
                return CheckoutSessionResult(
                    payment_attempt_id=2,
                    session_id=f"cs_unexpected_{order_id}",
                    checkout_url=f"https://checkout.stripe.com/pay/cs_unexpected_{order_id}",
                    status="open",
                )

        def cancel_order_payment(
            self,
            *,
            order_id: int,
            reason: str = "cancel",
            order_expires_at: object = None,
        ) -> CancelPaymentResult:
            with lock:
                cancelling_orders.add(order_id)
            cancel_entered.set()
            assert allow_cancel_finish.wait(timeout=5.0)
            with lock:
                cancelling_orders.discard(order_id)
                closed_orders.add(order_id)
                return CancelPaymentResult(
                    status="can_cancel", action="expired_in_stripe"
                )

    coordinator = ClosureCoordinatingPaymentsClient()
    app.dependency_overrides[get_payments_client] = lambda: coordinator
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            cancel_future = pool.submit(
                lambda: client.post(
                    f"/orders/{order.id}/cancel",
                    headers={"Authorization": f"Bearer {token}"},
                )
            )
            assert cancel_entered.wait(timeout=5.0)

            # Идва payment.expired в Commerce (резервът не е изтекъл -> Commerce върти към attempt_2)
            with session_factory() as event_session:
                svc = OrderService(
                    order_repository=OrderRepository(event_session),
                    cart_repository=CartRepository(event_session),
                    product_repository=ProductRepository(event_session),
                    user_repository=UserRepository(event_session),
                    session=event_session,
                    payments_client=coordinator,
                )
                outcome = svc.apply_payment_event(
                    make_payment_event(
                        event_type="payment.expired",
                        order_id=order.id,
                        attempt_key=f"order_{order.id}_attempt_1",
                        amount_minor=4000,
                    )
                )
                assert outcome == "expired"

            # Заявка за checkout с новия ключ attempt_2 -> отказан 409, не създава Stripe сесия
            r_retry_checkout = client.post(
                f"/orders/{order.id}/checkout-session",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r_retry_checkout.status_code == 409
            assert r_retry_checkout.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"

            allow_cancel_finish.set()
            r_cancel = cancel_future.result(timeout=5.0)
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    assert r_cancel.status_code == 200
    assert r_cancel.json()["status"] == "cancelled"
    assert seen_attempt_keys == [f"order_{order.id}_attempt_2"]
    assert stripe_sessions_created == 0
    with session_factory() as verify_session:
        refreshed_product = verify_session.get(Product, product.id)
        refreshed_order = verify_session.get(Order, order.id)
        assert refreshed_product is not None and refreshed_product.stock == 10
        assert refreshed_order is not None and refreshed_order.status == "cancelled"


def test_interrupted_cancel_retry_after_recovery_restores_stock_once_without_second_link(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Сценарий 1 (Commerce): прекъсната отмяна (502 -> cancelling) блокира нов checkout и след възстановяване връща stock веднъж."""
    user, token = _create_user_and_token(
        db_session, email="interrupted_cancel_commerce@example.com"
    )
    product = _create_product(
        db_session, "InterruptedCancelProd", price_minor=2000, stock=10
    )
    order = _create_order(db_session, user.id, product, quantity=3)  # stock=7

    state = {"phase": "crashed_before_finalize", "stripe_sessions_created": 0}

    class RecoveringPaymentsClient:
        def create_checkout_session(
            self,
            *,
            order_id: int,
            attempt_key: str,
            amount_minor: int,
            currency: str,
            line_items: list[dict[str, object]],
            order_expires_at: object,
        ) -> CheckoutSessionResult:
            if state["phase"] == "crashed_before_finalize":
                raise RemotePaymentConflict(
                    current_status="cancelling",
                    message="Cancellation recovery pending",
                )
            if state["phase"] == "recovered_cancelled":
                raise RemotePaymentConflict(
                    current_status="cancelled",
                    message="Order cancelled",
                )
            state["stripe_sessions_created"] = int(state["stripe_sessions_created"]) + 1
            raise AssertionError("Should never create a second Stripe session")

        def cancel_order_payment(
            self,
            *,
            order_id: int,
            reason: str = "cancel",
            order_expires_at: object = None,
        ) -> CancelPaymentResult:
            if state["phase"] == "crashed_before_finalize":
                raise PaymentGatewayError("Payments process crashed mid-cancel")
            return CancelPaymentResult(status="can_cancel", action="expired_in_stripe")

    app.dependency_overrides[get_payments_client] = RecoveringPaymentsClient
    try:
        # 1. Прекъснат cancel -> 502, stock остава резервиран (7), checkout е забранен (409)
        r_crash = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_crash.status_code == 502

        r_blocked = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_blocked.status_code == 409
        with session_factory() as s:
            assert s.get(Product, product.id).stock == 7  # type: ignore[union-attr]
            assert s.get(Order, order.id).status == "pending_payment"  # type: ignore[union-attr]

        # 2. След сверката в Payments отмяната е финализирана -> повторен cancel връща stock веднъж
        state["phase"] = "recovered_cancelled"
        r_recovered_cancel = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_recovered_cancel.status_code == 200
        assert r_recovered_cancel.json()["status"] == "cancelled"

        r_repeat_cancel = client.post(
            f"/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_repeat_cancel.status_code == 200
        r_after_checkout = client.post(
            f"/orders/{order.id}/checkout-session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_after_checkout.status_code == 409
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    assert state["stripe_sessions_created"] == 0
    with session_factory() as verify_session:
        refreshed_product = verify_session.get(Product, product.id)
        refreshed_order = verify_session.get(Order, order.id)
        assert refreshed_product is not None and refreshed_product.stock == 10
        assert refreshed_order is not None and refreshed_order.status == "cancelled"


def test_lost_response_cancellation_saves_cancelled_outcome_and_restores_stock(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Загубен HTTP отговор при отмяна до checkout: събитието payment.cancelled затваря поръчката и връща стоката."""
    user, token = _create_user_and_token(db_session, email="closure_lost@example.com")
    product = _create_product(db_session, "ClosureLostProd", price_minor=1500, stock=10)
    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=25)

    order_lost = _create_order(
        db_session, user.id, product, quantity=2, expires_at=future
    )  # stock: 10 -> 8
    mock_lost = MagicMock(spec=PaymentsClient)
    mock_lost.cancel_order_payment.side_effect = PaymentGatewayError(
        "HTTP response dropped after Payments commit"
    )
    app.dependency_overrides[get_payments_client] = lambda: mock_lost
    try:
        r_lost = client.post(
            f"/orders/{order_lost.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_lost.status_code == 502
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    with session_factory() as s_lost:
        o_lost = s_lost.get(Order, order_lost.id)
        p_lost = s_lost.get(Product, product.id)
        assert o_lost is not None and o_lost.status == "pending_payment"
        assert p_lost is not None and p_lost.stock == 8
        svc_lost = OrderService(
            order_repository=OrderRepository(s_lost),
            cart_repository=CartRepository(s_lost),
            product_repository=ProductRepository(s_lost),
            user_repository=UserRepository(s_lost),
            session=s_lost,
        )
        assert (
            svc_lost.apply_payment_event(
                make_payment_event(
                    event_type="payment.cancelled",
                    order_id=order_lost.id,
                    payment_attempt_id=None,
                    attempt_key=None,
                    stripe_session_id=None,
                )
            )
            == "cancelled"
        )
    with session_factory() as s_chk:
        assert s_chk.get(Order, order_lost.id).status == "cancelled"  # type: ignore[union-attr]
        assert s_chk.get(Product, product.id).stock == 10  # type: ignore[union-attr]


def test_terminal_closure_conflict_expired_prevents_manual_cancel_and_restores_stock(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Payments вече е финализирал expired -> ръчен cancel получава 409 и payment.expired завършва поръчката с връщане на наличност."""
    user, token = _create_user_and_token(
        db_session, email="closure_expired@example.com"
    )
    product = _create_product(
        db_session, "ClosureExpiredProd", price_minor=1500, stock=10
    )
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)

    order_exp = _create_order(
        db_session, user.id, product, quantity=3, expires_at=past
    )  # stock: 10 -> 7
    mock_exp = MagicMock(spec=PaymentsClient)
    mock_exp.cancel_order_payment.side_effect = RemotePaymentConflict(
        current_status="expired",
        message="Order payment closure is already finalized with status 'expired'",
    )
    app.dependency_overrides[get_payments_client] = lambda: mock_exp
    try:
        r_exp_cancel = client.post(
            f"/orders/{order_exp.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_exp_cancel.status_code == 409
        assert r_exp_cancel.json()["detail"]["code"] == "ORDER_STATUS_CONFLICT"
    finally:
        app.dependency_overrides.pop(get_payments_client, None)

    with session_factory() as s_exp:
        svc_exp = OrderService(
            order_repository=OrderRepository(s_exp),
            cart_repository=CartRepository(s_exp),
            product_repository=ProductRepository(s_exp),
            user_repository=UserRepository(s_exp),
            session=s_exp,
        )
        assert (
            svc_exp.apply_payment_event(
                make_payment_event(
                    event_type="payment.expired",
                    order_id=order_exp.id,
                    amount_minor=4500,
                )
            )
            == "expired"
        )
    with session_factory() as s_chk2:
        assert s_chk2.get(Order, order_exp.id).status == "expired"  # type: ignore[union-attr]
        assert s_chk2.get(Product, product.id).stock == 10  # type: ignore[union-attr]


def test_terminal_closure_conflict_cancelled_defers_reconciliation_and_preserves_stock(
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Payments вече е финализирал cancelled -> reconcile_orders получава 409, отлага поръчката и запазва наличността."""
    user, _token = _create_user_and_token(
        db_session, email="closure_cancelled@example.com"
    )
    product = _create_product(
        db_session, "ClosureCancelledProd", price_minor=1500, stock=10
    )
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)

    order_can = _create_order(
        db_session, user.id, product, quantity=2, expires_at=past
    )  # stock: 10 -> 8
    mock_can = MagicMock(spec=PaymentsClient)
    mock_can.cancel_order_payment.side_effect = RemotePaymentConflict(
        current_status="cancelled",
        message="Order payment closure is already finalized with status 'cancelled'",
    )
    with session_factory() as s_rec:
        svc_rec = OrderService(
            order_repository=OrderRepository(s_rec),
            cart_repository=CartRepository(s_rec),
            product_repository=ProductRepository(s_rec),
            user_repository=UserRepository(s_rec),
            session=s_rec,
            payments_client=mock_can,
        )
        stats = svc_rec.reconcile_expired_orders()
        assert stats["deferred"] >= 1
    with session_factory() as s_chk3:
        assert s_chk3.get(Order, order_can.id).status == "pending_payment"  # type: ignore[union-attr]
        assert s_chk3.get(Product, product.id).stock == 8  # type: ignore[union-attr]
