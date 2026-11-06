"""CLI команда за периодична или ръчна сверка на просрочени и висящи поръчки.

Правила:
- Срокът (expires_at) означава „време е да проверим“, а не „може директно да върнем стоката“.
- Ако няма опит за плащане, просроченият резерв се освобождава локално.
- Ако сесията е отворена в Stripe, първо се потвърждава прекратяването ѝ в Stripe.
- Ако Stripe потвърди плащане -> поръчката се маркира като paid.
- Ако плащането все още се обработва (async payment) или Stripe е недостъпен -> резервът се запазва.
- Повторен или паралелен запуск не удвоява наличностите.
"""

import argparse
import sys
from pathlib import Path

# Добавяне на корена на commerce към sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import db
from app.modules.accounts.repository import UserRepository
from app.modules.cart.repository import CartRepository
from app.modules.catalog.repository import ProductRepository
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from app.modules.orders.stripe_client import StripeClient


def run_reconciliation() -> dict[str, int]:
    """Изпълнява сверка на висящите поръчки и връща агрегирана статистика."""
    with db.session() as session:
        order_repo = OrderRepository(session)
        cart_repo = CartRepository(session)
        product_repo = ProductRepository(session)
        user_repo = UserRepository(session)
        stripe_client = StripeClient()

        service = OrderService(
            order_repository=order_repo,
            cart_repository=cart_repo,
            product_repository=product_repo,
            user_repository=user_repo,
            session=session,
            stripe_client=stripe_client,
        )

        return service.reconcile_unresolved_orders()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Сверка на просрочени и висящи Stripe поръчки."
    )
    _ = parser.parse_args()

    stats = run_reconciliation()
    print(
        f"Reconciliation completed: "
        f"checked={stats['checked']}, "
        f"expired={stats['expired']}, "
        f"paid={stats['paid']}, "
        f"deferred={stats['deferred']}"
    )


if __name__ == "__main__":
    main()

