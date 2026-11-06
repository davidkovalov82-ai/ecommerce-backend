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
