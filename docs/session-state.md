# Session State

- **Текуща задача**: Етап К2: продуктови категории (справочник, привързване към стоки, филтриране и права).
- **Реализирано**:
  - Справочник категории: `GET /categories` (публичен), `POST /categories`, `PATCH /categories/{id}`, `DELETE /categories/{id}` (само админ: 401/403). Уникалност по `lower(trim(name))` в PostgreSQL и 409 при дубликат. Защита при триене на категория със стоки (409 `CATEGORY_IN_USE`).
  - Стоки: `products.category_id` (FK `categories.id` RESTRICT, nullable). 3 случая в `PATCH /products/{id}`: липсващ ключ (без промяна), валидно ID (обновяване), явен `null` (изчистване). 422 при невалидно ID без частични промени. Всички схеми връщат `category_id: int | None`.
  - Филтриране: `GET /products?category_id=...` филтрира в SQL преди limit/offset, запазва сортировката по ID и правилата за видимост (активна стока и активен продавач). Празен `items: []` при липса на активни стоки.
  - Миграция `d5e6f7a8b9c0_add_categories_and_product_category_id.py` и 9 интеграционни теста в `tests/catalog/test_categories.py`.
- **Проверено / Резултати**:
  - `uv run --locked pytest -q`: 271 passed, 0 failed (100% успех на пълния suite).
  - `uv run --locked ruff check .` и `uv run --locked ruff format --check .`: 0 грешки.
  - `alembic upgrade head`: успешно приложена миграция на реална PostgreSQL база.
- **Непроверено**: Няма.
- **Работещи команди**:
  - `cd services/commerce && uv run --locked ruff check . && uv run --locked ruff format --check .`
  - `cd services/commerce && uv run --locked alembic upgrade head`
  - `cd services/commerce && uv run --locked pytest -q`
- **Засегнати файлове**: `app/modules/catalog/*`, `app/main.py`, `migrations/.../d5e6f7a8b9c0_*.py`, `tests/conftest.py`, `tests/catalog/test_categories.py`.
- **Следваща стъпка**: Потребителски преглед и ръчен commit / push на тестовия модул и обновения session state.
