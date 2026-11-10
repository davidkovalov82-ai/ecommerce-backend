# Session State

- **Текуща задача**: Етап К4: отделяне на Notifications в независим сервис (RabbitMQ + Email/SMS), запазване на исторически Outbox и изолирани E2E проверки.
- **Реализирано**:
  - `services/commerce`: Outbox схема с `event_id`, `event_version`, `occurred_at`, `published_at` (`f7a8b9c0d1e2`: `'sent'` → `'legacy_sent'`, `'pending'` запазени), RabbitMQ `EventPublisher` с `mandatory=True` + confirms, без извеждане на контакти в логовете.
  - `services/notifications`: самостоятелен сервис с отделна БД/роля `notifications`, Alembic миграция, RabbitMQ consumer (`commit` преди `basic_ack`, `UNIQUE(event_id, channel)`, DLQ за невалидни/непознати версии), разделни Email (SMTP/Mailpit) и SMS (Twilio адаптер в `test`/контрактен режим) цикли с лезинг.
  - Оркестрация и CI: `compose.yaml` (db-init, rabbitmq, commerce-outbox, notifications), изолирана команда `make test-k4-e2e` (`compose.e2e.yaml`), `.github/workflows/ci.yml` и `README.md`.
- **Проверено / Резултати**:
  - `services/commerce`: `ruff check .`, `ruff format --check .` и `pytest -q` → **295 passed**.
  - `services/notifications`: `ruff check .`, `ruff format --check .` и `pytest -q` → **21 passed**.
  - `scripts/`: `ruff check` и `ruff format --check` → 0 грешки; `make test-k4-e2e` → всички 5 сценария + изолация + DLQ преминаха (без спиране на работния Compose).
- **Непроверено**: Реална платена SMS доставка към външен Twilio API в продукционен `SMS_MODE=live` режим (тествано само чрез HTTP контрактен/локален mock режим).
- **Работещи команди**:
  - `docker compose --env-file services/commerce/.env up --build -d`
  - `make test-k4-e2e`
  - `cd services/commerce && uv run --locked pytest -q` и `cd services/notifications && uv run --locked pytest -q`
- **Засегнати файлове**: `services/commerce/*`, `services/notifications/*`, `compose.yaml`, `compose.e2e.yaml`, `Makefile`, `scripts/*`, `.github/workflows/ci.yml`, `README.md`.
- **Следваща стъпка**: Потребителски преглед и ръчен commit / push от клон `feat/notifications-service`.
