"""CLI команда на Commerce за приемане на входящи платежни събития от RabbitMQ."""

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import db
from app.modules.orders.payment_consumer import RabbitMQPaymentEventConsumer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Приемане на payment.* събития от RabbitMQ в Commerce."
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    args = parser.parse_args()

    consumer = RabbitMQPaymentEventConsumer(session_factory=db.session)
    if args.once and not args.daemon:
        count = consumer.consume_available()
        print(f"Payment events consumed: {count}")
        return

    stop_event = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        consumer.run(stop_event=stop_event)
    finally:
        db.close()


if __name__ == "__main__":
    main()
