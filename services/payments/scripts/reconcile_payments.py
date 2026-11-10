"""CLI команда за сверка на незавършени плащания в Payments със Stripe."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import db
from app.repository import PaymentRepository
from app.service import PaymentService
from app.stripe_client import StripeClient


def run_reconciliation() -> dict[str, int]:
    """Изпълнява сверка на активните/висящите плащания и връща агрегирана статистика."""
    with db.session() as session:
        service = PaymentService(
            repository=PaymentRepository(session),
            session=session,
            stripe_client=StripeClient(),
        )
        return service.reconcile_unresolved_payments()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Сверка на активните/изтеклите Stripe плащания в Payments."
    )
    _ = parser.parse_args()
    stats = run_reconciliation()
    print(
        f"Payments reconciliation completed: "
        f"checked={stats['checked']}, "
        f"expired={stats['expired']}, "
        f"paid={stats['paid']}, "
        f"deferred={stats['deferred']}"
    )


if __name__ == "__main__":
    main()
