"""CLI команда за публикуване на чакащите outbox събития към RabbitMQ."""

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

# Добавяне на корена на commerce към sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import db
from app.modules.orders.outbox_processor import OutboxProcessor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


def run_outbox_processing(limit: int = 50) -> dict[str, int]:
    """Обработва една партида чакащи outbox събития и връща статистика."""
    processor = OutboxProcessor(session_factory=db.session)
    try:
        return processor.process_pending_events(limit=limit)
    finally:
        processor.close()


def run_outbox_daemon(limit: int = 50, poll_interval: float = 1.0) -> None:
    """Непрекъснат цикъл за публикуване на outbox събития към RabbitMQ."""
    stop_requested = False

    def _handle_stop(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    processor = OutboxProcessor(session_factory=db.session)
    try:
        while not stop_requested:
            stats = processor.process_pending_events(limit=limit)
            if stats["processed"] == 0 or stats["failed"] > 0:
                time.sleep(poll_interval)
    finally:
        processor.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Публикуване на чакащите outbox събития от Commerce към RabbitMQ."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Максимален брой събития в една партида (по подразбиране: 50)",
    )
    parser.add_argument(
        "--daemon",
        "--watch",
        action="store_true",
        dest="daemon",
        help="Работи в непрекъснат режим до получаване на SIGTERM/SIGINT",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Интервал на изчакване в секунди между проверките в daemon режим",
    )
    args = parser.parse_args()

    if args.daemon:
        run_outbox_daemon(limit=args.limit, poll_interval=args.poll_interval)
        return

    stats = run_outbox_processing(limit=args.limit)
    print(
        f"Outbox processing completed: "
        f"processed={stats['processed']}, "
        f"published={stats['published']}, "
        f"failed={stats['failed']}"
    )


if __name__ == "__main__":
    main()
