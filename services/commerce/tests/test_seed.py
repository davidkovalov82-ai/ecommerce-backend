from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.catalog.models import Product
from scripts.seed_products import seed


def test_seed_products_idempotent(db_session: Session) -> None:
    """Изпълняваме зареждането два пъти -> няма допълнителни дубликати."""
    initial_count = db_session.scalar(select(func.count(Product.id))) or 0
    assert initial_count == 1  # От първоначалната миграция

    # Първо изпълнение: зарежда 100 продукта
    added_first = seed(db_session)
    assert added_first == 100

    count_after_first = db_session.scalar(select(func.count(Product.id))) or 0
    assert count_after_first == initial_count + 100

    # Второ изпълнение: засича съществуващите записи и не добавя дубликати
    added_second = seed(db_session)
    assert added_second == 0

    count_after_second = db_session.scalar(select(func.count(Product.id))) or 0
    assert count_after_second == count_after_first
