from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.catalog.models import Product
from scripts.seed_products import generate_100_products, seed


def test_seed_products_idempotent(db_session: Session) -> None:
    """Изпълняваме зареждането два пъти -> няма допълнителни дубликати."""
    initial_count = db_session.scalar(select(func.count(Product.id))) or 0
    assert initial_count == 1  # От първоначалната миграция (Notebook)

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


def test_seed_products_with_unrelated_products(db_session: Session) -> None:
    """При наличие на несвързани ръчни продукти, seed не бива да се отказва, а да зареди всички 100 демопродукта."""
    # Добавяме 2 ръчно създадени несвързани продукта (общо стават 3 с Notebook)
    custom_p1 = Product(
        title="Custom Handmade Wooden Desk",
        price_minor=85000,
        currency="EUR",
        stock=2,
        active=True,
    )
    custom_p2 = Product(
        title="Bespoke Mechanical Macro Pad",
        price_minor=12000,
        currency="EUR",
        stock=5,
        active=True,
    )
    db_session.add_all([custom_p1, custom_p2])
    db_session.commit()

    initial_count = db_session.scalar(select(func.count(Product.id))) or 0
    assert initial_count == 3  # Notebook + 2 ръчни продукта

    # Изпълняваме seed: въпреки че existing_count > 1, трябва да разпознае, че демоданните липсват
    added = seed(db_session)
    assert added == 100

    final_count = db_session.scalar(select(func.count(Product.id))) or 0
    assert final_count == 3 + 100

    # Повторният запуск добавя 0
    assert seed(db_session) == 0
    assert (db_session.scalar(select(func.count(Product.id))) or 0) == final_count


def test_seed_products_with_partially_present_demo_data(db_session: Session) -> None:
    """При частично налични демопродукти, seed трябва да добави само липсващите."""
    demo_catalog = generate_100_products()
    # Вмъкваме предварително първите 25 демопродукта
    partially_inserted = demo_catalog[:25]
    for data in partially_inserted:
        db_session.add(Product(**data))
    db_session.commit()

    count_before_seed = db_session.scalar(select(func.count(Product.id))) or 0
    assert count_before_seed == 1 + 25  # Notebook + 25 демопродукта

    # Seed трябва да добави точно липсващите 75 продукта
    added = seed(db_session)
    assert added == 75

    total_after_seed = db_session.scalar(select(func.count(Product.id))) or 0
    assert total_after_seed == 1 + 100

    # Проверка, че всяко заглавие от 100-те демопродукта присъства точно веднъж
    all_demo_titles = [str(p["title"]) for p in demo_catalog]
    existing_in_db = db_session.scalars(
        select(Product.title).where(Product.title.in_(all_demo_titles))
    ).all()
    assert len(existing_in_db) == 100
    assert len(set(existing_in_db)) == 100

    # Повторен опит не добавя нищо
    assert seed(db_session) == 0
    assert (db_session.scalar(select(func.count(Product.id))) or 0) == total_after_seed
