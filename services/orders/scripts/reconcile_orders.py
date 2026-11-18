"""CLI команда за сверка на просрочени поръчки в Commerce (без директна комуникация със Stripe)."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import db
from app.modules.accounts.repository import UserRepository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.payments_client import HttpPaymentsClient
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService


def run_reconciliation() -> dict[str, int]:
    """Сверява просрочените поръчки с Payments сервиса и връща статистика."""
    with db.session() as session:
        service = OrderService(
            order_repository=OrderRepository(session),
            cart_repository=CartRepository(session),
            product_repository=ProductRepository(session),
            user_repository=UserRepository(session),
            session=session,
            payments_client=HttpPaymentsClient(),
        )
        return service.reconcile_expired_orders()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Сверка на просрочени неплатени поръчки в Commerce с Payments."
    )
    _ = parser.parse_args()
    stats = run_reconciliation()
    print(
        f"Commerce expired-order reconciliation completed: "
        f"checked={stats['checked']}, "
        f"expired={stats['expired']}, "
        f"paid={stats['paid']}, "
        f"deferred={stats['deferred']}"
    )


if __name__ == "__main__":
    main()
