# Session State

- **Текуща задача**: Реализация на завършен модул за количка в `services/commerce/app/modules/cart`.
- **Реализирано**:
  - Alembic миграция `9c17d651dd68_create_cart_items_table` с композитен PK, FKs (CASCADE/RESTRICT) и CHECK constraint.
  - CRUD и бизнес логика за количка: `GET /cart`, `PUT /cart/items/{product_id}`, `DELETE /cart/items/{product_id}`.
  - Атомарен PostgreSQL upsert (`ON CONFLICT DO UPDATE`), групово извличане (`ProductRepository.get_by_ids`).
  - Пълна изолация между потребители по JWT `CurrentUserDep`, динамични цени без блокиране на наличности.
  - Документация в `README.md` и изчерпателни тестове в `test_cart.py` (23 теста) и `test_openapi.py`.
- **Непроверено**: Интеграция с бъдещия checkout сценарий (orders/payments).
- **Работещи команди**:
  - `cd services/commerce && uv run --locked pytest`
  - `cd services/commerce && uv run --locked ruff check . && uv run --locked ruff format --check .`
  - `docker compose --env-file services/commerce/.env up -d --build api`
- **Засегнати файлове**: `app/modules/cart/*`, `catalog/repository.py`, `app/main.py`, `migrations/*`, `tests/*`, `README.md`.
- **Следваща стъпка**: Реализация на създаване и жизнен цикъл на поръчка (`orders`).
