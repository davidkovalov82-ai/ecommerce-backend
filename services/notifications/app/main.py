"""Входна точка на независимия сервис Notifications (AMQP consumer + channel worker)."""

import argparse
import logging
import signal
import threading

from app.config import settings
from app.consumer import RabbitMQEventConsumer
from app.db import db
from app.worker import DeliveryWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def run_service(
    *,
    consumer_only: bool = False,
    worker_only: bool = False,
    once: bool = False,
) -> None:
    """Стартира AMQP консуматора и независимия worker за Email/SMS."""
    consumer = RabbitMQEventConsumer(session_factory=db.session)
    worker = DeliveryWorker(session_factory=db.session)

    if once:
        if not worker_only:
            consumer.consume_available()
        if not consumer_only:
            worker.process_once()
        return

    stop_event = threading.Event()

    def _handle_stop(signum: int, _frame: object) -> None:
        logger.info("Received shutdown signal=%s; stopping notifications", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    worker_thread: threading.Thread | None = None
    if not consumer_only and not worker_only:
        worker_thread = threading.Thread(
            target=worker.run_loop,
            args=(stop_event,),
            kwargs={"poll_interval_seconds": settings.worker_poll_interval_seconds},
            daemon=True,
            name="notifications-delivery-worker",
        )
        worker_thread.start()

    try:
        if worker_only:
            worker.run_loop(
                stop_event,
                poll_interval_seconds=settings.worker_poll_interval_seconds,
            )
        else:
            consumer.run(stop_event=stop_event)
    finally:
        stop_event.set()
        if worker_thread is not None:
            worker_thread.join(timeout=5.0)
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Независим Notifications сервис (RabbitMQ consumer + Email/SMS worker)."
    )
    parser.add_argument(
        "--consumer-only",
        action="store_true",
        help="Стартира само AMQP консуматора",
    )
    parser.add_argument(
        "--worker-only",
        action="store_true",
        help="Стартира само worker-а за доставка по канали",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Изпълнява еднократна обработка и излиза",
    )
    args = parser.parse_args()
    run_service(
        consumer_only=args.consumer_only,
        worker_only=args.worker_only,
        once=args.once,
    )


if __name__ == "__main__":
    main()
