from typing import Any

import pytest
from fastapi.testclient import TestClient
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


def test_patch_product_without_token_returns_401() -> None:
    """PATCH /products/{id} без токен връща 401 AUTHENTICATION_REQUIRED с WWW-Authenticate header."""
    res = client.patch("/products/1", json={"price_minor": 1500})
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate") == "Bearer"
    assert res.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"


def test_patch_product_as_customer_on_foreign_product_returns_404(
    db_session: Session,
) -> None:
    """PATCH /products/{id} за чужд продукт от customer връща 404 PRODUCT_NOT_FOUND и не променя БД."""
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
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"

    db_session.expire_all()
    reloaded = db_session.get(Product, 1)
    assert reloaded is not None
    assert reloaded.price_minor == initial_price


def test_patch_product_as_admin_on_foreign_product_returns_404(
    db_session: Session,
) -> None:
    """Администраторските права сами по себе си не дават право за редакция на чужд продукт (връща 404)."""
    _, admin_token = _create_user_with_role(
        db_session, "admin_foreign@example.com", "admin"
    )

    res = client.patch(
        "/products/1",
        json={"price_minor": 99999},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"


def test_patch_product_price_only_keeps_other_fields(db_session: Session) -> None:
    """PATCH /products/{id} обновява само подадената цена на собствен продукт, като запазва останалите полета."""
    owner, token = _create_user_with_role(
        db_session, "owner_patch_price@example.com", "customer"
    )
    product = Product(
        title="Custom Item",
        price_minor=1200,
        currency="EUR",
        stock=10,
        active=True,
        seller_id=owner.id,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    res = client.patch(
        f"/products/{product.id}",
        json={"price_minor": 2500},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == product.id
    assert data["price_minor"] == 2500
    assert data["title"] == "Custom Item"  # непроменено
    assert data["currency"] == "EUR"  # непроменено
    assert data["stock"] == 10  # непроменено
    assert data["active"] is True  # непроменено
    assert data["seller_id"] == owner.id

    db_session.expire_all()
    p = db_session.get(Product, product.id)
    assert p is not None
    assert p.price_minor == 2500
    assert p.title == "Custom Item"


def test_patch_product_zero_and_false_values_applied_correctly(
    db_session: Session,
) -> None:
    """Стойности 0 и false се прилагат правилно и не се третират като липсващи."""
    owner, token = _create_user_with_role(
        db_session, "owner_patch_zeros@example.com", "customer"
    )
    product = Product(
        title="Zero Item",
        price_minor=1000,
        currency="EUR",
        stock=10,
        active=True,
        seller_id=owner.id,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    res = client.patch(
        f"/products/{product.id}",
        json={"price_minor": 0, "stock": 0, "active": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["price_minor"] == 0
    assert data["stock"] == 0
    assert data["active"] is False

    db_session.expire_all()
    p = db_session.get(Product, product.id)
    assert p is not None
    assert p.price_minor == 0
    assert p.stock == 0
    assert p.active is False


def test_patch_deactivate_product_hides_from_public_get(
    db_session: Session,
) -> None:
    """Деактивирането на собствен продукт (active=False) го скрива от публичните GET операции."""
    owner, token = _create_user_with_role(
        db_session, "owner_deactivate@example.com", "customer"
    )
    product = Product(
        title="Deactivatable Item",
        price_minor=1500,
        currency="EUR",
        stock=5,
        active=True,
        seller_id=owner.id,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    # 1. Проверяваме, че продуктът е видим публично
    get_before = client.get(f"/products/{product.id}")
    assert get_before.status_code == 200

    # 2. Деактивираме го през PATCH
    patch_res = client.patch(
        f"/products/{product.id}",
        json={"active": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["active"] is False

    # 3. Публичният GET /products/{id} връща 404
    get_after = client.get(f"/products/{product.id}")
    assert get_after.status_code == 404
    assert get_after.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"

    # 4. Публичният списък не включва деактивирания продукт
    list_res = client.get("/products")
    assert list_res.status_code == 200
    ids = [item["id"] for item in list_res.json()["items"]]
    assert product.id not in ids


def test_patch_reactivate_product_makes_visible_again(
    db_session: Session,
) -> None:
    """Повторното активиране на продукт (active=True) го прави видим за публичните GET операции."""
    owner, token = _create_user_with_role(
        db_session, "owner_reactivate@example.com", "customer"
    )
    product = Product(
        title="Reactivatable Item",
        price_minor=2000,
        currency="EUR",
        stock=5,
        active=False,
        seller_id=owner.id,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    # Проверка, че е скрит
    assert client.get(f"/products/{product.id}").status_code == 404

    # 2. Активираме го отново през PATCH
    patch_res = client.patch(
        f"/products/{product.id}",
        json={"active": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["active"] is True

    # 3. Публичният GET /products/{id} отново връща 200
    get_res = client.get(f"/products/{product.id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == product.id
    assert get_res.json()["active"] is True

    # 4. Включен е в публичния списък
    list_res = client.get("/products")
    assert list_res.status_code == 200
    ids = [item["id"] for item in list_res.json()["items"]]
    assert product.id in ids


def test_patch_inactive_product_can_be_edited_by_owner(
    db_session: Session,
) -> None:
    """Собственикът може да редактира заглавие и цена на неактивен продукт, без да го активира."""
    owner, token = _create_user_with_role(
        db_session, "owner_edit_inactive@example.com", "customer"
    )

    inactive_prod = Product(
        title="Discontinued Mouse",
        price_minor=1999,
        currency="EUR",
        stock=0,
        active=False,
        seller_id=owner.id,
    )
    db_session.add(inactive_prod)
    db_session.commit()
    db_session.refresh(inactive_prod)

    res = client.patch(
        f"/products/{inactive_prod.id}",
        json={"title": "  Updated Vintage Mouse  ", "price_minor": 2999},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["title"] == "Updated Vintage Mouse"
    assert data["price_minor"] == 2999
    assert data["active"] is False  # остава неактивен

    # Публичният GET все още връща 404 за него
    assert client.get(f"/products/{inactive_prod.id}").status_code == 404


def test_patch_cannot_transfer_ownership_returns_422(
    db_session: Session,
) -> None:
    """Опит за предаване на владение чрез seller_id в PATCH връща 422."""
    owner, token = _create_user_with_role(
        db_session, "owner_transfer@example.com", "customer"
    )
    product = Product(
        title="Untransferable Item",
        price_minor=1000,
        currency="EUR",
        stock=5,
        active=True,
        seller_id=owner.id,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    res = client.patch(
        f"/products/{product.id}",
        json={"seller_id": 9999},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 422


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
