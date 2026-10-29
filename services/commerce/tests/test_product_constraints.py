import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.catalog.models import Product


def test_negative_price_rejected(db_session: Session) -> None:
    """Отрицателната цена трябва да се отхвърли от PostgreSQL CheckConstraint."""
    product = Product(
        title="Invalid Product",
        price_minor=-1,
        currency="EUR",
        stock=10,
        active=True,
    )
    db_session.add(product)
    with pytest.raises(IntegrityError, match="ck_products_price_minor_non_negative"):
        db_session.flush()


def test_negative_stock_rejected(db_session: Session) -> None:
    """Отрицателната наличност трябва да се отхвърли от PostgreSQL CheckConstraint."""
    product = Product(
        title="Invalid Product",
        price_minor=1000,
        currency="EUR",
        stock=-1,
        active=True,
    )
    db_session.add(product)
    with pytest.raises(IntegrityError, match="ck_products_stock_non_negative"):
        db_session.flush()


def test_unsupported_currency_rejected(db_session: Session) -> None:
    """Валута, различна от EUR, трябва да се отхвърли от PostgreSQL CheckConstraint."""
    product = Product(
        title="Invalid Product",
        price_minor=1000,
        currency="USD",
        stock=10,
        active=True,
    )
    db_session.add(product)
    with pytest.raises(IntegrityError, match="ck_products_currency_eur"):
        db_session.flush()


def test_zero_price_and_zero_stock_allowed(db_session: Session) -> None:
    """Нулева цена (0) и нулева наличност (0) са позволени стойности."""
    product = Product(
        title="Free Out of Stock Sample",
        price_minor=0,
        currency="EUR",
        stock=0,
        active=True,
    )
    db_session.add(product)
    db_session.flush()

    # Проверяваме, че записът е успешно съхранен и четен
    saved = db_session.scalar(select(Product).where(Product.title == "Free Out of Stock Sample"))
    assert saved is not None
    assert saved.price_minor == 0
    assert saved.stock == 0
    assert saved.currency == "EUR"
