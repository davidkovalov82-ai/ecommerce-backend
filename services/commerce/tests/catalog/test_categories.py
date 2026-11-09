from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.modules.accounts.models import User
from app.modules.catalog.models import Category
from tests.orders.helpers import _create_user_and_token

client = TestClient(app)


def _create_admin(
    db_session: Session, email: str = "cat_admin@example.com"
) -> tuple[User, str]:
    """Помощна функция за създаване на администратор и генериране на валиден JWT токен."""
    return _create_user_and_token(db_session, email=email, role="admin")


def _create_customer(
    db_session: Session, email: str = "cat_customer@example.com"
) -> tuple[User, str]:
    """Помощна функция за създаване на обикновен потребител (клиент) и генериране на JWT токен."""
    return _create_user_and_token(db_session, email=email, role="customer")


def test_public_get_categories(db_session: Session) -> None:
    """GET /categories е публичен и връща списък със стабилна сортировка по ID възходящо."""
    # 1. Първоначално без категории
    res = client.get("/categories")
    assert res.status_code == 200
    assert res.json() == []

    # 2. Админ създава няколко категории
    _, admin_token = _create_admin(db_session)
    c1 = client.post(
        "/categories",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "Категория Б"},
    )
    assert c1.status_code == 201
    c2 = client.post(
        "/categories",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "Категория А"},
    )
    assert c2.status_code == 201

    # 3. Публичен достъп без авторизация
    res = client.get("/categories")
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 2
    assert items[0]["id"] < items[1]["id"]
    assert items[0]["name"] == "Категория Б"
    assert items[1]["name"] == "Категория А"


def test_admin_authorization_for_category_management(
    db_session: Session,
) -> None:
    """Само администратори имат достъп до POST, PATCH и DELETE /categories."""
    _, customer_token = _create_customer(db_session)
    _, admin_token = _create_admin(db_session)

    # 1. Без токен -> 401
    assert client.post("/categories", json={"name": "Test"}).status_code == 401
    assert client.patch("/categories/1", json={"name": "New"}).status_code == 401
    assert client.delete("/categories/1").status_code == 401

    # 2. Обикновен потребител -> 403
    headers_cust = {"Authorization": f"Bearer {customer_token}"}
    res_post = client.post("/categories", headers=headers_cust, json={"name": "Test"})
    assert res_post.status_code == 403
    assert res_post.json()["detail"]["code"] == "FORBIDDEN"

    res_patch = client.patch(
        "/categories/1", headers=headers_cust, json={"name": "New"}
    )
    assert res_patch.status_code == 403
    assert res_patch.json()["detail"]["code"] == "FORBIDDEN"

    res_del = client.delete("/categories/1", headers=headers_cust)
    assert res_del.status_code == 403
    assert res_del.json()["detail"]["code"] == "FORBIDDEN"

    # 3. Администратор има достъп
    headers_admin = {"Authorization": f"Bearer {admin_token}"}
    create_res = client.post(
        "/categories", headers=headers_admin, json={"name": "Мебели"}
    )
    assert create_res.status_code == 201
    cat_id = create_res.json()["id"]

    patch_res = client.patch(
        f"/categories/{cat_id}",
        headers=headers_admin,
        json={"name": "Офис мебели"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["name"] == "Офис мебели"

    del_res = client.delete(f"/categories/{cat_id}", headers=headers_admin)
    assert del_res.status_code == 204


def test_category_name_validation_and_trimming(db_session: Session) -> None:
    """Проверява изчистването на интервали и валидацията на непразно име."""
    _, admin_token = _create_admin(db_session)
    headers = {"Authorization": f"Bearer {admin_token}"}

    # 1. Изчистване на интервали в краищата
    res = client.post("/categories", headers=headers, json={"name": "  Електроника  "})
    assert res.status_code == 201
    cat_id = res.json()["id"]
    assert res.json()["name"] == "Електроника"

    # 2. Празно име или само интервали -> 422
    assert (
        client.post("/categories", headers=headers, json={"name": ""}).status_code
        == 422
    )
    assert (
        client.post("/categories", headers=headers, json={"name": "   "}).status_code
        == 422
    )
    assert (
        client.post("/categories", headers=headers, json={"name": None}).status_code
        == 422
    )
    assert client.post("/categories", headers=headers, json={}).status_code == 422

    # 3. Допълнителни непозволени полета -> 422
    assert (
        client.post(
            "/categories",
            headers=headers,
            json={"name": "Спорт", "extra": "invalid"},
        ).status_code
        == 422
    )

    # 4. Валидация при PATCH
    patch_trim = client.patch(
        f"/categories/{cat_id}",
        headers=headers,
        json={"name": "  Битова техника  "},
    )
    assert patch_trim.status_code == 200
    assert patch_trim.json()["name"] == "Битова техника"

    assert (
        client.patch(
            f"/categories/{cat_id}", headers=headers, json={"name": "   "}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/categories/{cat_id}",
            headers=headers,
            json={"name": "Ново", "unknown": 1},
        ).status_code
        == 422
    )


def test_category_name_uniqueness_and_duplicate_conflict(
    db_session: Session,
) -> None:
    """«Електроника» и « електроника » се считат за едно име. Дубликат връща 409."""
    _, admin_token = _create_admin(db_session)
    headers = {"Authorization": f"Bearer {admin_token}"}

    # 1. Създаваме първоначална категория
    res1 = client.post("/categories", headers=headers, json={"name": "Електроника"})
    assert res1.status_code == 201
    cat1_id = res1.json()["id"]

    # 2. Опит за създаване на дубликат с малки букви и интервали -> 409
    res_dup1 = client.post(
        "/categories", headers=headers, json={"name": "  електроника  "}
    )
    assert res_dup1.status_code == 409
    assert res_dup1.json()["detail"]["code"] == "CATEGORY_NAME_EXISTS"

    res_dup2 = client.post("/categories", headers=headers, json={"name": "ЕЛЕКТРОНИКА"})
    assert res_dup2.status_code == 409
    assert res_dup2.json()["detail"]["code"] == "CATEGORY_NAME_EXISTS"

    # 3. Създаваме втора категория
    res2 = client.post("/categories", headers=headers, json={"name": "Книги"})
    assert res2.status_code == 201
    cat2_id = res2.json()["id"]

    # 4. Преименуване на втората категория с името на първата -> 409
    res_patch_dup = client.patch(
        f"/categories/{cat2_id}",
        headers=headers,
        json={"name": "  електроника "},
    )
    assert res_patch_dup.status_code == 409
    assert res_patch_dup.json()["detail"]["code"] == "CATEGORY_NAME_EXISTS"

    # 5. Преименуване на първата категория със същото ѝ име -> 200 OK
    res_patch_same = client.patch(
        f"/categories/{cat1_id}",
        headers=headers,
        json={"name": "Електроника"},
    )
    assert res_patch_same.status_code == 200

    # 6. Преименуване на несъществуваща категория -> 404
    assert (
        client.patch(
            "/categories/99999", headers=headers, json={"name": "Непозната"}
        ).status_code
        == 404
    )


def test_concurrent_category_creation_uniqueness(db_session: Session) -> None:
    """Конкурентно създаване на категория с еднакво име гарантира точно един 201 и 409 за останалите."""
    _, admin_token = _create_admin(db_session)
    headers = {"Authorization": f"Bearer {admin_token}"}
    target_name = "Уникална Електроника"

    def _attempt_create(variant: str) -> int:
        local_client = TestClient(app)
        res = local_client.post("/categories", headers=headers, json={"name": variant})
        return res.status_code

    variants = [
        target_name,
        f"  {target_name.lower()}  ",
        target_name.upper(),
        f" {target_name} ",
    ]

    with ThreadPoolExecutor(max_workers=len(variants)) as executor:
        statuses = list(executor.map(_attempt_create, variants))

    assert statuses.count(201) == 1
    assert statuses.count(409) == len(variants) - 1

    # В базата данни трябва да има точно един запис
    cats = (
        db_session.query(Category).filter(Category.name.ilike(f"%{target_name}%")).all()
    )
    assert len(cats) == 1


def test_category_deletion_with_and_without_products(
    db_session: Session,
) -> None:
    """Ако към категорията е привързан поне един продукт (активен или не), DELETE връща 409.

    Продуктите не се променят.
    """
    _, admin_token = _create_admin(db_session)
    _, anna_token = _create_customer(db_session, email="anna_deletion_test@example.com")
    headers_admin = {"Authorization": f"Bearer {admin_token}"}
    headers_anna = {"Authorization": f"Bearer {anna_token}"}

    # 1. Изтриване на несъществуваща категория -> 404
    assert client.delete("/categories/99999", headers=headers_admin).status_code == 404

    # 2. Създаване и изтриване на празна категория -> 204
    res_cat = client.post(
        "/categories", headers=headers_admin, json={"name": "Празна категория"}
    )
    assert res_cat.status_code == 201
    cat_id = res_cat.json()["id"]

    del_res = client.delete(f"/categories/{cat_id}", headers=headers_admin)
    assert del_res.status_code == 204
    assert (
        client.delete(f"/categories/{cat_id}", headers=headers_admin).status_code == 404
    )

    # 3. Създаване на категория с продукт
    res_hardware = client.post(
        "/categories", headers=headers_admin, json={"name": "Хардуер"}
    )
    assert res_hardware.status_code == 201
    hw_id = res_hardware.json()["id"]

    prod_res = client.post(
        "/products",
        headers=headers_anna,
        json={
            "title": "USB Flash Drive",
            "price_minor": 1200,
            "currency": "EUR",
            "stock": 10,
            "category_id": hw_id,
        },
    )
    assert prod_res.status_code == 201
    prod_id = prod_res.json()["id"]
    assert prod_res.json()["category_id"] == hw_id

    # 4. Опит за изтриване на категорията, докато продуктът е активен -> 409
    del_blocked = client.delete(f"/categories/{hw_id}", headers=headers_admin)
    assert del_blocked.status_code == 409
    assert del_blocked.json()["detail"]["code"] == "CATEGORY_IN_USE"

    # Продуктът не е засегнат
    get_prod = client.get(f"/products/{prod_id}")
    assert get_prod.status_code == 200
    assert get_prod.json()["category_id"] == hw_id

    # 5. Деактивираме продукта (active = False)
    patch_prod = client.patch(
        f"/products/{prod_id}",
        headers=headers_anna,
        json={"active": False},
    )
    assert patch_prod.status_code == 200
    assert patch_prod.json()["active"] is False

    # Изтриването все още трябва да връща 409, дори когато продуктът е неактивен!
    del_blocked_inactive = client.delete(f"/categories/{hw_id}", headers=headers_admin)
    assert del_blocked_inactive.status_code == 409
    assert del_blocked_inactive.json()["detail"]["code"] == "CATEGORY_IN_USE"

    # 6. Изчистваме категорията на продукта (category_id = null)
    clear_cat = client.patch(
        f"/products/{prod_id}",
        headers=headers_anna,
        json={"category_id": None},
    )
    assert clear_cat.status_code == 200
    assert clear_cat.json()["category_id"] is None

    # Сега категорията е свободна и се изтрива успешно
    del_success = client.delete(f"/categories/{hw_id}", headers=headers_admin)
    assert del_success.status_code == 204


def test_product_category_assignment_and_patch_three_way_semantics(
    db_session: Session,
) -> None:
    """Тества 3-те случая за category_id в PATCH /products/{id}, строгия null за останалите и 422 за несъществуващо ID."""
    _, admin_token = _create_admin(db_session)
    _, anna_token = _create_customer(db_session, email="anna_patch_test@example.com")
    _, boris_token = _create_customer(db_session, email="boris_patch_test@example.com")
    headers_admin = {"Authorization": f"Bearer {admin_token}"}
    headers_anna = {"Authorization": f"Bearer {anna_token}"}
    headers_boris = {"Authorization": f"Bearer {boris_token}"}

    # Създаваме две категории
    cat1_id = client.post(
        "/categories", headers=headers_admin, json={"name": "Категория 1"}
    ).json()["id"]
    cat2_id = client.post(
        "/categories", headers=headers_admin, json={"name": "Категория 2"}
    ).json()["id"]

    # 1. POST /products с несъществуващ category_id -> 422
    assert (
        client.post(
            "/products",
            headers=headers_anna,
            json={
                "title": "Invalid Category Product",
                "price_minor": 1000,
                "currency": "EUR",
                "stock": 5,
                "category_id": 99999,
            },
        ).status_code
        == 422
    )

    # 2. POST /products без category_id -> category_id е null
    p_null = client.post(
        "/products",
        headers=headers_anna,
        json={
            "title": "No Category Product",
            "price_minor": 1000,
            "currency": "EUR",
            "stock": 5,
        },
    ).json()
    assert p_null["category_id"] is None

    # 3. POST /products с валидна категория
    p = client.post(
        "/products",
        headers=headers_anna,
        json={
            "title": "Anna Lamp",
            "price_minor": 3000,
            "currency": "EUR",
            "stock": 5,
            "category_id": cat1_id,
        },
    ).json()
    prod_id = p["id"]
    assert p["category_id"] == cat1_id

    # 4. Случай 1 в PATCH: ключът category_id отсъства -> категорията остава непроменена
    patch_omitted = client.patch(
        f"/products/{prod_id}",
        headers=headers_anna,
        json={"price_minor": 3500},
    )
    assert patch_omitted.status_code == 200
    assert patch_omitted.json()["price_minor"] == 3500
    assert patch_omitted.json()["category_id"] == cat1_id

    # 5. Случай 2 в PATCH: подадено е валидно ново ID -> категорията се променя
    patch_new_cat = client.patch(
        f"/products/{prod_id}",
        headers=headers_anna,
        json={"category_id": cat2_id},
    )
    assert patch_new_cat.status_code == 200
    assert patch_new_cat.json()["category_id"] == cat2_id

    # 6. Случай 3 в PATCH: подаден е изричен null -> категорията се изчиства
    patch_clear_cat = client.patch(
        f"/products/{prod_id}",
        headers=headers_anna,
        json={"category_id": None},
    )
    assert patch_clear_cat.status_code == 200
    assert patch_clear_cat.json()["category_id"] is None

    # 7. Несъществуващо ID на категория в PATCH -> 422 без частични промени
    patch_invalid_cat = client.patch(
        f"/products/{prod_id}",
        headers=headers_anna,
        json={"category_id": 99999, "price_minor": 9999},
    )
    assert patch_invalid_cat.status_code == 422
    assert patch_invalid_cat.json()["detail"]["code"] == "INVALID_CATEGORY"

    # Проверяваме, че цената НЕ е променена (няма частични промени)
    check_prod = client.get(f"/products/{prod_id}").json()
    assert check_prod["price_minor"] == 3500
    assert check_prod["category_id"] is None

    # 8. Забрана за null при останалите полета -> 422
    assert (
        client.patch(
            f"/products/{prod_id}",
            headers=headers_anna,
            json={"title": None},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/products/{prod_id}",
            headers=headers_anna,
            json={"price_minor": None},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/products/{prod_id}",
            headers=headers_anna,
            json={"currency": None},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/products/{prod_id}",
            headers=headers_anna,
            json={"stock": None},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/products/{prod_id}",
            headers=headers_anna,
            json={"active": None},
        ).status_code
        == 422
    )

    # 9. Чужд продукт не може да се редактира -> 404
    assert (
        client.patch(
            f"/products/{prod_id}",
            headers=headers_boris,
            json={"category_id": cat1_id},
        ).status_code
        == 404
    )


def test_catalog_filtering_by_category_and_pagination(
    db_session: Session,
) -> None:
    """GET /products?category_id=... филтрира в SQL преди limit/offset, запазва сортировката и видимостта."""
    _, admin_token = _create_admin(db_session)
    anna_user, anna_token = _create_customer(
        db_session, email="anna_filter_test@example.com"
    )
    headers_admin = {"Authorization": f"Bearer {admin_token}"}
    headers_anna = {"Authorization": f"Bearer {anna_token}"}

    c1_id = client.post(
        "/categories", headers=headers_admin, json={"name": "Книги"}
    ).json()["id"]
    c2_id = client.post(
        "/categories", headers=headers_admin, json={"name": "Техника"}
    ).json()["id"]
    c_empty_id = client.post(
        "/categories", headers=headers_admin, json={"name": "Дрехи"}
    ).json()["id"]

    # Създаваме 3 продукта в C1 (2 активни, 1 неактивен)
    p1 = client.post(
        "/products",
        headers=headers_anna,
        json={
            "title": "Книга 1",
            "price_minor": 1000,
            "currency": "EUR",
            "stock": 10,
            "category_id": c1_id,
        },
    ).json()["id"]
    p2 = client.post(
        "/products",
        headers=headers_anna,
        json={
            "title": "Книга 2",
            "price_minor": 2000,
            "currency": "EUR",
            "stock": 10,
            "category_id": c1_id,
        },
    ).json()["id"]
    client.post(
        "/products",
        headers=headers_anna,
        json={
            "title": "Книга 3 Скрита",
            "price_minor": 3000,
            "currency": "EUR",
            "stock": 10,
            "active": False,
            "category_id": c1_id,
        },
    )

    # 1 продукт в C2
    p4 = client.post(
        "/products",
        headers=headers_anna,
        json={
            "title": "Лаптоп",
            "price_minor": 50000,
            "currency": "EUR",
            "stock": 5,
            "category_id": c2_id,
        },
    ).json()["id"]

    # 1. Филтриране по C1 -> виждат се само активните p1 и p2
    res_c1 = client.get(f"/products?category_id={c1_id}")
    assert res_c1.status_code == 200
    items_c1 = res_c1.json()["items"]
    assert len(items_c1) == 2
    assert [x["id"] for x in items_c1] == [p1, p2]
    assert all(x["category_id"] == c1_id for x in items_c1)

    # 2. Филтриране по C2 -> вижда се само p4
    res_c2 = client.get(f"/products?category_id={c2_id}")
    assert res_c2.status_code == 200
    items_c2 = res_c2.json()["items"]
    assert len(items_c2) == 1
    assert items_c2[0]["id"] == p4
    assert items_c2[0]["category_id"] == c2_id

    # 3. Съществуваща категория без активни продукти -> 200 с items: []
    res_empty = client.get(f"/products?category_id={c_empty_id}")
    assert res_empty.status_code == 200
    assert res_empty.json()["items"] == []

    # 4. Несъществуваща категория -> 200 с items: []
    res_non_existent = client.get("/products?category_id=99999")
    assert res_non_existent.status_code == 200
    assert res_non_existent.json()["items"] == []

    # 5. Без филтър -> виждат се всички активни продукти (вкл. legacy product id=1)
    res_all = client.get("/products")
    assert res_all.status_code == 200
    ids_all = [x["id"] for x in res_all.json()["items"]]
    assert p1 in ids_all and p2 in ids_all and p4 in ids_all

    # 6. Пагинация с SQL филтрация: limit=1, offset=0 и offset=1
    page1 = client.get(f"/products?category_id={c1_id}&limit=1&offset=0").json()
    assert len(page1["items"]) == 1
    assert page1["items"][0]["id"] == p1

    page2 = client.get(f"/products?category_id={c1_id}&limit=1&offset=1").json()
    assert len(page2["items"]) == 1
    assert page2["items"][0]["id"] == p2

    page3 = client.get(f"/products?category_id={c1_id}&limit=1&offset=2").json()
    assert len(page3["items"]) == 0

    # 7. Влияние на блокиран продавач: ако Анна бъде блокирана, нейните продукти изчезват от филтъра
    anna_user.status = "blocked"
    db_session.commit()

    res_blocked = client.get(f"/products?category_id={c1_id}")
    assert res_blocked.status_code == 200
    assert res_blocked.json()["items"] == []


def test_end_to_end_acceptance_scenario(db_session: Session) -> None:
    """Сквозной тест (критерий за приемане):

    Админ създава 2 категории -> Анна публикува стока -> Борис филтрира каталога ->
    Анна премества стоката в другата категория -> стоката се премества в филтрите.
    """
    _, admin_token = _create_admin(db_session)
    _, anna_token = _create_customer(db_session, email="anna_acceptance@example.com")
    _, boris_token = _create_customer(db_session, email="boris_acceptance@example.com")

    headers_admin = {"Authorization": f"Bearer {admin_token}"}
    headers_anna = {"Authorization": f"Bearer {anna_token}"}
    headers_boris = {"Authorization": f"Bearer {boris_token}"}

    # 1. Админ създава 2 категории: "Книги" и "Техника"
    cat_books = client.post(
        "/categories", headers=headers_admin, json={"name": "Книги"}
    ).json()
    cat_tech = client.post(
        "/categories", headers=headers_admin, json={"name": "Техника"}
    ).json()
    books_id = cat_books["id"]
    tech_id = cat_tech["id"]

    # 2. Анна публикува продукт "Learning Python" в категория "Книги"
    prod = client.post(
        "/products",
        headers=headers_anna,
        json={
            "title": "Learning Python",
            "price_minor": 4500,
            "currency": "EUR",
            "stock": 7,
            "category_id": books_id,
        },
    ).json()
    prod_id = prod["id"]
    assert prod["category_id"] == books_id

    # 3. Борис филтрира каталога по "Книги" и "Техника"
    search_books = client.get(
        f"/products?category_id={books_id}", headers=headers_boris
    ).json()
    assert len(search_books["items"]) == 1
    assert search_books["items"][0]["id"] == prod_id
    assert search_books["items"][0]["title"] == "Learning Python"

    search_tech = client.get(
        f"/products?category_id={tech_id}", headers=headers_boris
    ).json()
    assert len(search_tech["items"]) == 0

    # 4. Анна пренасочва продукта в категория "Техника"
    update_res = client.patch(
        f"/products/{prod_id}",
        headers=headers_anna,
        json={"category_id": tech_id},
    )
    assert update_res.status_code == 200
    assert update_res.json()["category_id"] == tech_id

    # 5. Борис проверява отново филтрите: продуктът се е преместил в "Техника"
    search_books_after = client.get(
        f"/products?category_id={books_id}", headers=headers_boris
    ).json()
    assert len(search_books_after["items"]) == 0

    search_tech_after = client.get(
        f"/products?category_id={tech_id}", headers=headers_boris
    ).json()
    assert len(search_tech_after["items"]) == 1
    assert search_tech_after["items"][0]["id"] == prod_id
    assert search_tech_after["items"][0]["category_id"] == tech_id
