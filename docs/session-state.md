# Session State

- **Текуща задача**: K6.2 — отделяне на Catalog в самостоятелен микросервис services/catalog на чист стенд.
- **Реализирано**:
  - `services/catalog`: самостоятелен FastAPI сървис, PostgreSQL БД `catalog`, Alembic миграции, RabbitMQ consumer за потребителски събития (`users.events` -> `catalog.user_events`), атомно и идемпотентно управление на наличности (резервация и освобождаване).
  - `services/commerce`: премахнати директни SQL връзки към Catalog; Cart и Orders комуникират през `CatalogClient`; запазени публични URL адреси; Orders пази исторически снимки (`title`, `price_minor`, `seller_id`).
  - Миграция `k5f6a7b8c9d0`: премахнати foreign key ограничения към `products.id` от `cart_items` и `order_items`.
  - Инфраструктура: добавени `catalog` и `catalog-user-consumer` в `compose.yaml` и `compose.e2e.yaml`, скрипт `scripts/catalog_e2e_verify.py` и команда `make test-catalog-e2e`.
- **Проверено локално**:
  - `make test-catalog-e2e`: успешно на чист стенд (1 наличност -> 2 купувача -> точно една 201, една 409, остатък 0, потвърдена идемпотентност и снимка в поръчката);
  - `services/catalog` pytest: 3 passed; `services/commerce` pytest: 310 passed; `services/users` pytest: 57 passed; Ruff чист.
- **Непроверено**: няма.
- **Работещи команди**: `make test-catalog-e2e`, `cd services/commerce && uv run --locked pytest -q`, `cd services/catalog && uv run pytest -q`.
- **Засегнати файлове**: `services/catalog/*`, `services/commerce/*`, `compose.yaml`, `compose.e2e.yaml`, `scripts/*`, `Makefile`.
- **Следваща стъпка**: Преглед и публикуване на промените от потребителя.
