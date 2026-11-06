"""CLI команда за обработка на чакащите outbox събития (Transactional Outbox)."""

import argparse
import sys
from pathlib import Path

# Добавяне на корена на commerce към sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import db
from app.modules.orders.outbox_processor import OutboxProcessor


def run_outbox_processing(limit: int = 50) -> dict[str, int]:
    """Обработва чакащите събития и връща агрегирана статистика."""
    processor = OutboxProcessor(session_factory=db.session)
    return processor.process_pending_events(limit=limit)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Обработка на чакащите outbox събития и изпращане на имейл известия."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Максимален брой събития за обработка наведнъж (по подразбиране: 50)",
    )
    args = parser.parse_args()

    stats = run_outbox_processing(limit=args.limit)
    print(
        f"Outbox processing completed: "
        f"processed={stats['processed']}, "
        f"sent={stats['sent']}, "
        f"failed={stats['failed']}"
    )


if __name__ == "__main__":
    main()
