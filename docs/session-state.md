# Session State

- **Текуща задача**: K6.3 — отделяне на Cart в самостоятелен микросервис services/cart на чист стенд.
- **Реализирано**:
  - Отстранени регресии: затворени фалшиви `X-User-*` заглавки без JWT в Catalog (връща 401); коригиран `scripts/users_e2e_verify.py` към Catalog DB; форматиран `app/config.py`.
  - `services/cart`: FastAPI, PostgreSQL БД `cart`, Alembic миграции, Docker compose услуга (порт 8004/58004). Запазени GET/PUT/DELETE `/cart` по верифициран JWT токен.
  - Cart съхранява само състав и количества; цени/наличности извлича от Catalog; добавянето не резервира наличност.
  - Orders извлича количката през `GET /internal/cart/{user_id}`, резервира през Catalog и изчиства поръчаните позиции през `POST /internal/cart/{user_id}/clear-purchased`.
  - Премахнато директно четене/запис на `cart_items` от Orders; пълна идемпотентност при повторен Idempotency-Key.
- **Проверено**:
  - `make test-cart-e2e` (`bash scripts/test_cart_e2e.sh`): 100% успешно на чист изолиран стенд.
  - Pytest: `services/cart` (6 passed), `services/commerce` (310 passed). Ruff: 100% чист и форматиран.
- **Непроверено**: няма.
- **Работещи команди**: `make test-cart-e2e`, `cd services/cart && uv run --locked pytest`, `cd services/commerce && uv run --locked pytest`.
- **Засегнати файлове**: `services/cart/*`, `services/commerce/*`, `services/catalog/*`, `compose.yaml`, `compose.e2e.yaml`, `scripts/*`.
- **Следваща стъпка**: Потребителят да прегледа и комитне промените.
