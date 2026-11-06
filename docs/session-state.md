# Session State

- **Текуща задача**: Stripe Checkout (sandbox), webhook и отказ от неплатена поръчка в `feat/payments`.
- **Реализирано**:
  - `stripe>=16.1.0` в dependencies; модели `StripeCheckoutSession` и `StripeProcessedEvent`.
  - Alembic миграция `e2b83a741f90` за таблици `stripe_checkout_sessions` и `stripe_processed_events`.
  - `POST /orders/{id}/checkout-session`: валидация на EUR лимити, връща Stripe Checkout URL, идемпотентно връщане на отворена сесия без задържане на DB lock при външен call.
  - `POST /webhooks/stripe`: проверка на `Stripe-Signature` над raw bytes, маркиране `paid` при `checkout.session.completed` (без повторно сваляне на stock), дедупликация на събития.
  - `POST /orders/{id}/cancel`: прекратяване на отворена Stripe сесия, защита от race condition (webhook побеждава -> 409), възстановяване на stock вкл. за деактивирани продукти.
  - Добавени 19 теста в `test_stripe_checkout.py`, тестове в `test_openapi.py` и `test_migration_sequence.py`.
- **Проверено / Резултати**:
  - Всички 199 теста в пълния pytest suite преминават успешно.
  - 100% успешен Ruff check и format check (`ruff check .`, `ruff format --check .`).
- **Работещи команди**:
  - `cd services/commerce && uv run --locked pytest -q`
  - `cd services/commerce && uv run --locked ruff check . && uv run --locked ruff format --check .`
- **Засегнати файлове**: `app/modules/orders/*`, `migrations/*`, `tests/*`, `README.md`, `docs/session-state.md`.
- **Следваща стъпка**: Личен преглед, git commit и push от потребителя; ръчен тест със `stripe listen`.
