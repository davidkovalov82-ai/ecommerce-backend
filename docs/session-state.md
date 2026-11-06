# Session State

- **Текуща задача**: Реализация на симулирано плащане и отказ от поръчка върху клон `feat/payments`.
- **Реализирано**:
  - Модел `PaymentAttempt` и разширяване на `ck_orders_status_valid` към `pending_payment`, `paid`, `cancelled`.
  - Alembic миграция `c74a9d821e5f` с частичен уникален индекс за 1 успешен опит на поръчка.
  - Локален `PaymentSimulator` и service/repository транзакции със заключване `FOR UPDATE`.
  - `POST /orders/{id}/payments` (идемпотентност с `Idempotency-Key`, 201/200/409) и `POST /orders/{id}/cancel` (възстановяване на stock вкл. за деактивирани продукти, 200/409).
  - Написани 15 интеграционни и конкурентни теста в `test_payments.py` и миграционен тест за съществуващи поръчки.
  - Успешни проверки: 179/179 теста в pytest и 100% Ruff lint & format check.
- **Оставащи ограничения / Непроверено**:
  - Липсва автоматично изтичане (auto-expiration) на неплатени поръчки (таймери/worker).
  - GitHub CI предстои след личен push на потребителя.
- **Работещи команди**:
  - `cd services/commerce && uv run --locked pytest -q`
  - `cd services/commerce && uv run --locked ruff check . && uv run --locked ruff format --check .`
- **Засегнати файлове**: `app/modules/orders/*`, `migrations/versions/c74a9d821e5f_*`, `tests/*`, `README.md`, `docs/session-state.md`.
- **Следваща стъпка**: Личен преглед, git commit и push на промените от потребителя.
