from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.accounts.models import User
from app.modules.accounts.tokens import create_access_token
from app.modules.catalog.models import Product

client = TestClient(app)
COMMERCE_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI_PATH = str(COMMERCE_DIR / "alembic.ini")
ALEMBIC_SCRIPT_LOCATION = str(COMMERCE_DIR / "migrations")


def _create_user(
    db_session: Session, email: str, role: str = "customer"
) -> tuple[User, str]:
    user = User(
        email=email,
        password_hash="test_argon2_hash",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    token = create_access_token(user_id=user.id, secret=settings.jwt_secret)
    return user, token


def test_two_sellers_publish_and_isolation_scenario(db_session: Session) -> None:
    """Сценарий: Анна и Борис публикуват артикули; взаимна изолация при редакция."""
    anna, anna_token = _create_user(db_session, "anna@example.com")
    boris, boris_token = _create_user(db_session, "boris@example.com")
    _admin, admin_token = _create_user(
        db_session, "admin_mod@example.com", role="admin"
    )

    # 1. Анна публикува стока
    res_anna = client.post(
        "/products",
        json={
            "title": "Anna Mechanical Keyboard",
            "price_minor": 10000,
            "currency": "EUR",
            "stock": 5,
            "active": True,
        },
        headers={"Authorization": f"Bearer {anna_token}"},
    )
    assert res_anna.status_code == 201
    anna_prod = res_anna.json()
    assert anna_prod["seller_id"] == anna.id
    anna_prod_id = anna_prod["id"]

    # 2. Борис публикува стока
    res_boris = client.post(
        "/products",
        json={
            "title": "Boris Ergonomic Mouse",
            "price_minor": 5000,
            "currency": "EUR",
            "stock": 10,
            "active": True,
        },
        headers={"Authorization": f"Bearer {boris_token}"},
    )
    assert res_boris.status_code == 201
    boris_prod = res_boris.json()
    assert boris_prod["seller_id"] == boris.id
    boris_prod_id = boris_prod["id"]

    # 3. И двата активни продукта се виждат в публичния каталог
    public_catalog = client.get("/products")
    assert public_catalog.status_code == 200
    catalog_ids = [item["id"] for item in public_catalog.json()["items"]]
    assert anna_prod_id in catalog_ids
    assert boris_prod_id in catalog_ids

    # 4. Борис се опитва да промени цената на стоката на Анна -> 404 Not Found
    res_boris_hack = client.patch(
        f"/products/{anna_prod_id}",
        json={"price_minor": 1},
        headers={"Authorization": f"Bearer {boris_token}"},
    )
    assert res_boris_hack.status_code == 404
    assert res_boris_hack.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"

    # Проверка, че цената на Анна не е променена
    db_session.expire_all()
    p_in_db = db_session.get(Product, anna_prod_id)
    assert p_in_db is not None
    assert p_in_db.price_minor == 10000

    # 5. Администратор се опитва да редактира стоката на Анна през API -> 404 Not Found
    res_admin_edit = client.patch(
        f"/products/{anna_prod_id}",
        json={"price_minor": 20000},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_admin_edit.status_code == 404
    assert res_admin_edit.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"

    # 6. Анна успешно редактира собствената си цена -> 200 OK
    res_anna_patch = client.patch(
        f"/products/{anna_prod_id}",
        json={"price_minor": 12000},
        headers={"Authorization": f"Bearer {anna_token}"},
    )
    assert res_anna_patch.status_code == 200
    assert res_anna_patch.json()["price_minor"] == 12000

    db_session.expire_all()
    assert db_session.get(Product, anna_prod_id).price_minor == 12000


def test_deactivation_and_visibility_in_my_products(db_session: Session) -> None:
    """Деактивирането скрива продукта от публичния каталог, но го запазва в личния списък."""
    _anna, anna_token = _create_user(db_session, "anna_deact@example.com")
    _boris, boris_token = _create_user(db_session, "boris_deact@example.com")

    # Анна създава продукт
    create_res = client.post(
        "/products",
        json={
            "title": "Discontinued Keyboard",
            "price_minor": 8000,
            "currency": "EUR",
            "stock": 3,
            "active": True,
        },
        headers={"Authorization": f"Bearer {anna_token}"},
    )
    assert create_res.status_code == 201
    prod_id = create_res.json()["id"]

    # Деактивира го
    deact_res = client.patch(
        f"/products/{prod_id}",
        json={"active": False},
        headers={"Authorization": f"Bearer {anna_token}"},
    )
    assert deact_res.status_code == 200
    assert deact_res.json()["active"] is False

    # Публично GET /products/{id} връща 404
    assert client.get(f"/products/{prod_id}").status_code == 404

    # Публичният списък не го съдържа
    public_res = client.get("/products")
    assert prod_id not in [p["id"] for p in public_res.json()["items"]]

    # В личния списък на Анна продуктът ПРИСЪСТВА със статус active=False
    anna_my_products = client.get(
        "/users/me/products",
        headers={"Authorization": f"Bearer {anna_token}"},
    )
    assert anna_my_products.status_code == 200
    anna_items = anna_my_products.json()["items"]
    assert any(item["id"] == prod_id and item["active"] is False for item in anna_items)

    # В личния списък на Борис продуктът на Анна НЕ присъства
    boris_my_products = client.get(
        "/users/me/products",
        headers={"Authorization": f"Bearer {boris_token}"},
    )
    assert boris_my_products.status_code == 200
    boris_items = boris_my_products.json()["items"]
    assert all(item["id"] != prod_id for item in boris_items)


def test_seller_id_cannot_be_injected(db_session: Session) -> None:
    """Клиентът не може да прехвърли или инжектира seller_id през POST или PATCH (връща 422)."""
    _user, token = _create_user(db_session, "injector@example.com")

    # POST с seller_id
    res_post = client.post(
        "/products",
        json={
            "title": "Hack Product",
            "price_minor": 1000,
            "currency": "EUR",
            "stock": 2,
            "seller_id": 999,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_post.status_code == 422

    # Създаваме нормален продукт
    res_valid = client.post(
        "/products",
        json={
            "title": "Valid Product",
            "price_minor": 1000,
            "currency": "EUR",
            "stock": 2,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_valid.status_code == 201
    prod_id = res_valid.json()["id"]

    # PATCH с seller_id
    res_patch = client.patch(
        f"/products/{prod_id}",
        json={"seller_id": 888},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_patch.status_code == 422


def test_users_me_products_auth_and_pagination(db_session: Session) -> None:
    """GET /users/me/products изисква автентикация и поддържа коректна пагинация."""
    # 1. Без токен -> 401
    unauth = client.get("/users/me/products")
    assert unauth.status_code == 401
    assert unauth.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"

    _user, token = _create_user(db_session, "paginator@example.com")

    # Създаваме 5 продукта за потребителя
    created_ids = []
    for i in range(5):
        r = client.post(
            "/products",
            json={
                "title": f"Item {i}",
                "price_minor": 1000 + i,
                "currency": "EUR",
                "stock": 5,
                "active": i % 2 == 0,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 201
        created_ids.append(r.json()["id"])

    # Страница 1 (limit=2, offset=0)
    page1 = client.get(
        "/users/me/products?limit=2&offset=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert page1.status_code == 200
    data1 = page1.json()
    assert len(data1["items"]) == 2
    assert [x["id"] for x in data1["items"]] == created_ids[:2]
    assert data1["limit"] == 2
    assert data1["offset"] == 0

    # Страница 2 (limit=2, offset=2)
    page2 = client.get(
        "/users/me/products?limit=2&offset=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert page2.status_code == 200
    data2 = page2.json()
    assert len(data2["items"]) == 2
    assert [x["id"] for x in data2["items"]] == created_ids[2:4]


def test_migration_on_database_with_existing_product() -> None:
    """Миграцията върху БД със съществуващи продукти запазва данните им и ги присвоява на техническия акаунт."""
    temp_db_name = "commerce_temp_k1_mig_test"
    admin_engine = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")

    with admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {temp_db_name}"))

    temp_db_url = (
        f"postgresql+psycopg://{settings.db_user}:{settings.db_password}"
        f"@{settings.db_host}:{settings.db_port}/{temp_db_name}"
    )
    temp_engine = create_engine(temp_db_url)

    try:
        alembic_cfg = Config(ALEMBIC_INI_PATH)
        alembic_cfg.set_main_option("script_location", ALEMBIC_SCRIPT_LOCATION)
        alembic_cfg.set_main_option("sqlalchemy.url", temp_db_url)

        # 1. Прилагаме миграциите до bd4c339e0dfa (точно преди новата миграция)
        command.upgrade(alembic_cfg, "bd4c339e0dfa")

        # 2. Вмъкваме продукт без seller_id директно с SQL
        with temp_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO products (id, title, price_minor, currency, stock, active) "
                    "VALUES (42, 'Pre-migration Classic Laptop', 89900, 'EUR', 7, true)"
                )
            )

        # 3. Прилагаме новата миграция (head)
        command.upgrade(alembic_cfg, "head")

        # 4. Проверяваме данните след миграцията
        with Session(temp_engine) as session:
            prod = session.get(Product, 42)
            assert prod is not None
            assert prod.id == 42
            assert prod.title == "Pre-migration Classic Laptop"
            assert prod.price_minor == 89900
            assert prod.currency == "EUR"
            assert prod.stock == 7
            assert prod.active is True
            assert prod.seller_id is not None

            # Проверяваме техническия потребител
            tech_user = session.get(User, prod.seller_id)
            assert tech_user is not None
            assert tech_user.email == "legacy-catalog@internal.ecommerce.com"
            assert tech_user.password_hash == "!disabled"
            assert tech_user.role == "customer"

        # 5. Опит за вход като legacy-catalog@internal.ecommerce.com трябва да върне 401
        # (входът е напълно невъзможен)
        login_res = client.post(
            "/auth/login",
            json={
                "email": "legacy-catalog@internal.ecommerce.com",
                "password": "any_attempted_password",
            },
        )
        assert login_res.status_code == 401
        assert login_res.json()["detail"]["code"] == "INVALID_CREDENTIALS"

    finally:
        temp_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {temp_db_name} WITH (FORCE)"))
        admin_engine.dispose()
