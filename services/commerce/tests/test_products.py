from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.modules.accounts.models import User
from app.modules.accounts.tokens import create_access_token
from app.modules.catalog.models import Product

client = TestClient(app)


def test_get_existing_product() -> None:
    """Критерий 1: GET /products/1 връща 200 и шестте полета на продукта."""
    response = client.get("/products/1")
    assert response.status_code == 200
    assert response.json() == {
        "id": 1,
        "title": "Notebook",
        "price_minor": 1299,
        "currency": "EUR",
        "stock": 10,
        "active": True,
    }


def test_get_missing_product() -> None:
    """Критерий 2: GET /products/999 връща 404 с договорената структура на detail."""
    response = client.get("/products/999")
    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "code": "PRODUCT_NOT_FOUND",
            "message": "Product not found",
        }
    }


def test_get_inactive_product_returns_404(db_session: Session) -> None:
    """Публичният детайл по ID връща 404 за неактивен продукт."""
    inactive_product = Product(
        title="Archived Old Product",
        price_minor=1999,
        currency="EUR",
        stock=0,
        active=False,
    )
    db_session.add(inactive_product)
    db_session.commit()
    db_session.refresh(inactive_product)

    response = client.get(f"/products/{inactive_product.id}")
    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "code": "PRODUCT_NOT_FOUND",
            "message": "Product not found",
        }
    }


def test_get_invalid_product_id() -> None:
    """Критерий 3: GET /products/abc връща 422 Unprocessable Entity."""
    response = client.get("/products/abc")
    assert response.status_code == 422


def test_list_active_products(db_session: Session) -> None:
    """Тест 1: GET /products връща списък с активни продукти и пълната им структура."""
    p1 = Product(
        title="Wireless Mouse",
        price_minor=2500,
        currency="EUR",
        stock=15,
        active=True,
    )
    p2 = Product(
        title="Mechanical Keyboard",
        price_minor=8900,
        currency="EUR",
        stock=8,
        active=True,
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    response = client.get("/products")
    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 20
    assert data["offset"] == 0

    items = data["items"]
    assert len(items) >= 3  # id=1 (Notebook) + 2 нови продукта
    titles = [item["title"] for item in items]
    assert "Notebook" in titles
    assert "Wireless Mouse" in titles
    assert "Mechanical Keyboard" in titles

    for item in items:
        assert item["active"] is True
        assert isinstance(item["id"], int)
        assert isinstance(item["title"], str)
        assert isinstance(item["price_minor"], int)
        assert item["currency"] == "EUR"
        assert isinstance(item["stock"], int)


def test_list_products_excludes_inactive(db_session: Session) -> None:
    """Тест 2: GET /products изключва неактивните продукти от резултата."""
    active_prod = Product(
        title="Active Monitor",
        price_minor=19900,
        currency="EUR",
        stock=5,
        active=True,
    )
    inactive_prod = Product(
        title="Archived Monitor",
        price_minor=14900,
        currency="EUR",
        stock=0,
        active=False,
    )
    db_session.add_all([active_prod, inactive_prod])
    db_session.commit()

    response = client.get("/products")
    assert response.status_code == 200
    data = response.json()
    returned_ids = [item["id"] for item in data["items"]]
    returned_titles = [item["title"] for item in data["items"]]

    assert active_prod.id in returned_ids
    assert "Active Monitor" in returned_titles
    assert inactive_prod.id not in returned_ids
    assert "Archived Monitor" not in returned_titles
    assert all(item["active"] is True for item in data["items"])


def test_list_products_ordering_and_pagination(db_session: Session) -> None:
    """Тест 3: GET /products спазва възходящо подреждане по ID и коректни limit и offset."""
    prods = [
        Product(
            title=f"Catalog Item {i}",
            price_minor=1000 * i,
            currency="EUR",
            stock=i,
            active=True,
        )
        for i in range(1, 6)
    ]
    db_session.add_all(prods)
    db_session.commit()

    # 1. Проверка на възходящо подреждане по ID
    full_response = client.get("/products?limit=50&offset=0")
    assert full_response.status_code == 200
    full_items = full_response.json()["items"]
    ids = [item["id"] for item in full_items]
    assert ids == sorted(ids)

    # 2. Проверка на limit
    limit_response = client.get("/products?limit=2&offset=0")
    assert limit_response.status_code == 200
    limit_data = limit_response.json()
    assert limit_data["limit"] == 2
    assert limit_data["offset"] == 0
    assert len(limit_data["items"]) == 2
    assert limit_data["items"] == full_items[:2]

    # 3. Проверка на offset
    offset_response = client.get("/products?limit=2&offset=2")
    assert offset_response.status_code == 200
    offset_data = offset_response.json()
    assert offset_data["limit"] == 2
    assert offset_data["offset"] == 2
    assert len(offset_data["items"]) == 2
    assert offset_data["items"] == full_items[2:4]


def test_list_products_empty_result(db_session: Session) -> None:
    """Тест 4: GET /products връща 200 с items=[] при липса на съвпадащи продукти."""
    # Вариант А: offset извън обхвата на съществуващите записи
    offset_response = client.get("/products?offset=500")
    assert offset_response.status_code == 200
    assert offset_response.json() == {
        "items": [],
        "limit": 20,
        "offset": 500,
    }

    # Вариант Б: няма активни продукти в базата
    db_session.execute(text("UPDATE products SET active = false WHERE active = true"))
    db_session.commit()
    try:
        empty_response = client.get("/products")
        assert empty_response.status_code == 200
        assert empty_response.json() == {
            "items": [],
            "limit": 20,
            "offset": 0,
        }
    finally:
        db_session.execute(text("UPDATE products SET active = true WHERE id = 1"))
        db_session.commit()


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": -1},
        {"limit": "abc"},
        {"offset": -1},
        {"offset": "xyz"},
    ],
)
def test_list_products_invalid_parameters(params: dict[str, Any]) -> None:
    """Тест 5: Невалидните параметри за limit и offset връщат 422 Unprocessable Entity."""
    response = client.get("/products", params=params)
    assert response.status_code == 422


def _create_user_with_role(
    db_session: Session, email: str, role: str
) -> tuple[User, str]:
    """Създава тестов потребител с конкретна роля и издава валиден JWT токен."""
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


def test_create_product_without_token_returns_401() -> None:
    """POST /products без Authorization хедър връща 401 AUTHENTICATION_REQUIRED."""
    payload = {
        "title": "Unauthorized Product",
        "price_minor": 1500,
        "currency": "EUR",
        "stock": 10,
        "active": True,
    }
    response = client.post("/products", json=payload)
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"
    assert response.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"


def test_create_product_as_customer_returns_403(db_session: Session) -> None:
    """POST /products от потребител с роля customer връща 403 FORBIDDEN и не добавя запис."""
    _, customer_token = _create_user_with_role(
        db_session, "customer_tester@example.com", "customer"
    )

    payload = {
        "title": "Customer Attempt Product",
        "price_minor": 2000,
        "currency": "EUR",
        "stock": 5,
        "active": True,
    }
    response = client.post(
        "/products",
        json=payload,
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert response.status_code == 403
    assert response.json() == {
        "detail": {
            "code": "FORBIDDEN",
            "message": "Admin privileges required",
        }
    }

    # Проверка, че продуктът не е записан в базата
    existing = db_session.execute(
        text("SELECT id FROM products WHERE title = 'Customer Attempt Product'")
    ).scalar()
    assert existing is None


def test_create_product_as_admin_returns_201_and_persists(
    db_session: Session,
) -> None:
    """POST /products от потребител с роля admin връща 201 и генерира ID в базата данни."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_tester@example.com", "admin"
    )

    payload = {
        "title": "  Professional Studio Mic  ",
        "price_minor": 24900,
        "currency": "EUR",
        "stock": 12,
        "active": True,
    }
    response = client.post(
        "/products",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 201
    data = response.json()
    assert (
        data["title"] == "Professional Studio Mic"
    )  # Външните интервали са премахнати
    assert data["price_minor"] == 24900
    assert data["currency"] == "EUR"
    assert data["stock"] == 12
    assert data["active"] is True
    assert isinstance(data["id"], int)
    assert data["id"] > 1

    # Проверка за директно съществуване в базата
    product_in_db = db_session.get(Product, data["id"])
    assert product_in_db is not None
    assert product_in_db.title == "Professional Studio Mic"


def test_created_active_product_visible_via_get(db_session: Session) -> None:
    """Създаденият активен продукт се вижда през GET /products и GET /products/{id}."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_visible@example.com", "admin"
    )

    payload = {
        "title": "Visible Active Product",
        "price_minor": 3500,
        "currency": "EUR",
        "stock": 7,
        "active": True,
    }
    create_res = client.post(
        "/products",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create_res.status_code == 201
    prod_id = create_res.json()["id"]

    # Преглед по ID
    get_res = client.get(f"/products/{prod_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == prod_id
    assert get_res.json()["title"] == "Visible Active Product"

    # Преглед в публичния списък
    list_res = client.get("/products")
    assert list_res.status_code == 200
    titles = [item["title"] for item in list_res.json()["items"]]
    assert "Visible Active Product" in titles


def test_created_inactive_product_hidden_from_public_get(
    db_session: Session,
) -> None:
    """Създаденият неактивен продукт (active=False) е скрит от публичните GET операции."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_inactive@example.com", "admin"
    )

    payload = {
        "title": "Hidden Inactive Product",
        "price_minor": 4500,
        "currency": "EUR",
        "stock": 0,
        "active": False,
    }
    create_res = client.post(
        "/products",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create_res.status_code == 201
    prod_id = create_res.json()["id"]

    # Публичният GET по ID връща 404
    get_res = client.get(f"/products/{prod_id}")
    assert get_res.status_code == 404
    assert get_res.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"

    # Публичният списък не включва неактивния продукт
    list_res = client.get("/products")
    assert list_res.status_code == 200
    titles = [item["title"] for item in list_res.json()["items"]]
    assert "Hidden Inactive Product" not in titles


def test_create_product_zero_price_and_stock_allowed(db_session: Session) -> None:
    """Нулева цена (0) и нулева наличност (0) са валидни стойности."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_zeros@example.com", "admin"
    )

    payload = {
        "title": "Free Promotional Sticker",
        "price_minor": 0,
        "currency": "EUR",
        "stock": 0,
        "active": True,
    }
    res = client.post(
        "/products",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 201
    assert res.json()["price_minor"] == 0
    assert res.json()["stock"] == 0


@pytest.mark.parametrize(
    "invalid_payload",
    [
        # Отрицателни стойности
        {
            "title": "Test",
            "price_minor": -1,
            "currency": "EUR",
            "stock": 5,
            "active": True,
        },
        {
            "title": "Test",
            "price_minor": 100,
            "currency": "EUR",
            "stock": -1,
            "active": True,
        },
        # Дробни числа (floats)
        {
            "title": "Test",
            "price_minor": 10.5,
            "currency": "EUR",
            "stock": 5,
            "active": True,
        },
        {
            "title": "Test",
            "price_minor": 100,
            "currency": "EUR",
            "stock": 2.5,
            "active": True,
        },
        # Boolean вместо integer
        {
            "title": "Test",
            "price_minor": True,
            "currency": "EUR",
            "stock": 5,
            "active": True,
        },
        {
            "title": "Test",
            "price_minor": 100,
            "currency": "EUR",
            "stock": False,
            "active": True,
        },
        # Прекалено големи числа (> 2147483647)
        {
            "title": "Test",
            "price_minor": 2_147_483_648,
            "currency": "EUR",
            "stock": 5,
            "active": True,
        },
        {
            "title": "Test",
            "price_minor": 100,
            "currency": "EUR",
            "stock": 2_147_483_648,
            "active": True,
        },
        # Празно заглавие
        {
            "title": "",
            "price_minor": 100,
            "currency": "EUR",
            "stock": 5,
            "active": True,
        },
        {
            "title": "   ",
            "price_minor": 100,
            "currency": "EUR",
            "stock": 5,
            "active": True,
        },
        # Друга валута
        {
            "title": "Test",
            "price_minor": 100,
            "currency": "USD",
            "stock": 5,
            "active": True,
        },
        {
            "title": "Test",
            "price_minor": 100,
            "currency": "BGN",
            "stock": 5,
            "active": True,
        },
        # Забранени непознати полета (вкл. подаден id)
        {
            "id": 999,
            "title": "Test",
            "price_minor": 100,
            "currency": "EUR",
            "stock": 5,
            "active": True,
        },
        {
            "title": "Test",
            "price_minor": 100,
            "currency": "EUR",
            "stock": 5,
            "active": True,
            "extra_field": "disallowed",
        },
    ],
)
def test_create_product_validation_errors(
    db_session: Session, invalid_payload: dict[str, Any]
) -> None:
    """Невалидните полета, типове или валута връщат 422 Unprocessable Entity."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_val@example.com", "admin"
    )

    res = client.post(
        "/products",
        json=invalid_payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 422


def test_registration_with_role_admin_forbidden() -> None:
    """Регистрация с подадено поле role=admin се отхвърля (422) и не позволява ескалация на права."""
    res = client.post(
        "/users",
        json={
            "email": "hacker@example.com",
            "password": "SuperSecretPassphrase123!",
            "role": "admin",
        },
    )
    assert res.status_code == 422


def test_role_change_in_db_recognized_with_existing_token(
    db_session: Session,
) -> None:
    """Промяна на роля от customer на admin в базата данни се отчита моментално със съществуващ токен."""
    user, token = _create_user_with_role(
        db_session, "promoted_later@example.com", "customer"
    )

    # Като customer заявката се отхвърля
    res_before = client.post(
        "/products",
        json={
            "title": "Test Prod",
            "price_minor": 100,
            "currency": "EUR",
            "stock": 1,
            "active": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_before.status_code == 403

    # Променяме ролята в базата
    user.role = "admin"
    db_session.commit()

    # Със СЪЩИЯ токен вече има достъп (тъй като get_current_user зарежда ролята от базата)
    res_after = client.post(
        "/products",
        json={
            "title": "Test Prod",
            "price_minor": 100,
            "currency": "EUR",
            "stock": 1,
            "active": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_after.status_code == 201


def test_role_revocation_in_db_recognized_with_existing_token(
    db_session: Session,
) -> None:
    """Отнемане на права (admin -> customer в БД): същият токен получава 403 при POST /products и не се създава продукт."""
    user, token = _create_user_with_role(
        db_session, "demoted_later@example.com", "admin"
    )

    # 1. Докато е admin, заявката е успешна
    res_before = client.post(
        "/products",
        json={
            "title": "Admin Initial Product",
            "price_minor": 100,
            "currency": "EUR",
            "stock": 1,
            "active": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_before.status_code == 201

    # 2. Отнемаме правата директно в базата данни (admin -> customer)
    user.role = "customer"
    db_session.commit()

    # 3. Със СЪЩИЯ токен вече получава 403 FORBIDDEN
    res_after = client.post(
        "/products",
        json={
            "title": "Blocked Customer Product",
            "price_minor": 100,
            "currency": "EUR",
            "stock": 1,
            "active": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_after.status_code == 403
    assert res_after.json()["detail"]["code"] == "FORBIDDEN"

    # 4. Проверяваме, че продуктът не е създаден в БД
    blocked_product = db_session.execute(
        text("SELECT id FROM products WHERE title = 'Blocked Customer Product'")
    ).scalar()
    assert blocked_product is None


# ==========================================
# PATCH /products/{product_id} тестове
# ==========================================


def test_patch_product_without_token_returns_401() -> None:
    """PATCH /products/{id} без токен връща 401 AUTHENTICATION_REQUIRED с WWW-Authenticate header."""
    res = client.patch("/products/1", json={"price_minor": 1500})
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"
    assert res.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"


def test_patch_product_as_customer_returns_403(db_session: Session) -> None:
    """PATCH /products/{id} от потребител с роля customer връща 403 FORBIDDEN и не променя БД."""
    _, customer_token = _create_user_with_role(
        db_session, "customer_patch@example.com", "customer"
    )

    initial_product = db_session.get(Product, 1)
    assert initial_product is not None
    initial_price = initial_product.price_minor

    res = client.patch(
        "/products/1",
        json={"price_minor": 99999},
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "FORBIDDEN"

    db_session.expire_all()
    reloaded = db_session.get(Product, 1)
    assert reloaded is not None
    assert reloaded.price_minor == initial_price


def test_patch_product_price_only_keeps_other_fields(db_session: Session) -> None:
    """PATCH /products/{id} обновява само подадената цена, като запазва останалите полета."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_patch_price@example.com", "admin"
    )

    res = client.patch(
        "/products/1",
        json={"price_minor": 2500},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == 1
    assert data["price_minor"] == 2500
    assert data["title"] == "Notebook"  # непроменено
    assert data["currency"] == "EUR"  # непроменено
    assert data["stock"] == 10  # непроменено
    assert data["active"] is True  # непроменено

    db_session.expire_all()
    p = db_session.get(Product, 1)
    assert p is not None
    assert p.price_minor == 2500
    assert p.title == "Notebook"


def test_patch_product_zero_and_false_values_applied_correctly(
    db_session: Session,
) -> None:
    """Стойности 0 и false се прилагат правилно и не се третират като липсващи."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_patch_zeros@example.com", "admin"
    )

    res = client.patch(
        "/products/1",
        json={"price_minor": 0, "stock": 0, "active": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["price_minor"] == 0
    assert data["stock"] == 0
    assert data["active"] is False

    db_session.expire_all()
    p = db_session.get(Product, 1)
    assert p is not None
    assert p.price_minor == 0
    assert p.stock == 0
    assert p.active is False


def test_patch_deactivate_product_hides_from_public_get(
    db_session: Session,
) -> None:
    """Деактивирането на продукт (active=False) го скрива от публичните GET операции."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_deactivate@example.com", "admin"
    )

    # 1. Проверяваме, че продукт 1 е видим публично
    get_before = client.get("/products/1")
    assert get_before.status_code == 200

    # 2. Деактивираме го през PATCH
    patch_res = client.patch(
        "/products/1",
        json={"active": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["active"] is False

    # 3. Публичният GET /products/1 връща 404
    get_after = client.get("/products/1")
    assert get_after.status_code == 404
    assert get_after.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"

    # 4. Публичният списък не включва деактивирания продукт
    list_res = client.get("/products")
    assert list_res.status_code == 200
    ids = [item["id"] for item in list_res.json()["items"]]
    assert 1 not in ids


def test_patch_reactivate_product_makes_visible_again(
    db_session: Session,
) -> None:
    """Повторното активиране на продукт (active=True) го прави видим за публичните GET операции."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_reactivate@example.com", "admin"
    )

    # 1. Първо деактивираме продукта
    p = db_session.get(Product, 1)
    assert p is not None
    p.active = False
    db_session.commit()

    # Проверка, че е скрит
    assert client.get("/products/1").status_code == 404

    # 2. Активираме го отново през PATCH
    patch_res = client.patch(
        "/products/1",
        json={"active": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["active"] is True

    # 3. Публичният GET /products/1 отново връща 200
    get_res = client.get("/products/1")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == 1
    assert get_res.json()["active"] is True

    # 4. Включен е в публичния списък
    list_res = client.get("/products")
    assert list_res.status_code == 200
    ids = [item["id"] for item in list_res.json()["items"]]
    assert 1 in ids


def test_patch_inactive_product_can_be_edited_by_admin(
    db_session: Session,
) -> None:
    """Администраторът може да редактира заглавие и цена на неактивен продукт, без да го активира."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_edit_inactive@example.com", "admin"
    )

    inactive_prod = Product(
        title="Discontinued Mouse",
        price_minor=1999,
        currency="EUR",
        stock=0,
        active=False,
    )
    db_session.add(inactive_prod)
    db_session.commit()
    db_session.refresh(inactive_prod)

    res = client.patch(
        f"/products/{inactive_prod.id}",
        json={"title": "  Updated Vintage Mouse  ", "price_minor": 2999},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["title"] == "Updated Vintage Mouse"
    assert data["price_minor"] == 2999
    assert data["active"] is False  # остава неактивен

    # Публичният GET все още връща 404 за него
    assert client.get(f"/products/{inactive_prod.id}").status_code == 404


def test_patch_nonexistent_product_returns_404(db_session: Session) -> None:
    """PATCH /products/{id} за несъществуващ продукт връща 404 PRODUCT_NOT_FOUND."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_patch_404@example.com", "admin"
    )

    res = client.patch(
        "/products/99999",
        json={"price_minor": 5000},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"


@pytest.mark.parametrize(
    "invalid_payload",
    [
        # Празен обект
        {},
        # Изрично null стойности
        {"title": None},
        {"price_minor": None},
        {"currency": None},
        {"stock": None},
        {"active": None},
        # Непознати полета (вкл. id)
        {"id": 10},
        {"unknown": "val"},
        {"price_minor": 100, "extra": 123},
        # Невалидни стойности за цена
        {"price_minor": -1},
        {"price_minor": 10.5},
        {"price_minor": True},
        {"price_minor": 2_147_483_648},
        # Невалидни стойности за наличност
        {"stock": -1},
        {"stock": 1.5},
        {"stock": False},
        {"stock": 2_147_483_648},
        # Невалидни стойности за заглавие
        {"title": ""},
        {"title": "   "},
        # Невалидна валута
        {"currency": "USD"},
        {"currency": "BGN"},
        # Невалидно active (не е boolean)
        {"active": "true"},
        {"active": 1},
        {"active": 0},
    ],
)
def test_patch_product_validation_errors(
    db_session: Session, invalid_payload: dict[str, Any]
) -> None:
    """Празен обект, null, непознати полета и невалидни стойности връщат 422 Unprocessable Entity."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_patch_val@example.com", "admin"
    )

    res = client.patch(
        "/products/1",
        json=invalid_payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 422


def test_patch_rejected_request_does_not_modify_db(
    db_session: Session,
) -> None:
    """Отхвърлена PATCH заявка гарантира транзакционен rollback и не променя данните в БД."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_rollback@example.com", "admin"
    )

    prod = db_session.get(Product, 1)
    assert prod is not None
    orig_title = prod.title
    orig_price = prod.price_minor
    orig_stock = prod.stock
    orig_active = prod.active

    # Заявка с едно валидно и едно невалидно поле (напр. extra id)
    res = client.patch(
        "/products/1",
        json={"price_minor": 99999, "id": 1},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 422

    db_session.expire_all()
    prod_after = db_session.get(Product, 1)
    assert prod_after is not None
    assert prod_after.title == orig_title
    assert prod_after.price_minor == orig_price
    assert prod_after.stock == orig_stock
    assert prod_after.active == orig_active
