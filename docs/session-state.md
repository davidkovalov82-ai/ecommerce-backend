# Session State

- **Текуща задача**: Доработка на Stripe Checkout: отложени методи (async payments), срокове (expires_at), сверка и live режим.
- **Реализирано**:
  - Модели и миграция `b4f8c2e1a9d0`: добавени `expires_at`, статуси `payment_processing`/`expired`, колона `mode` в сесиите.
  - Webhook & 4 събития: обработка на `completed`, `async_payment_succeeded`, `async_payment_failed`, `expired`, разхождения (`discrepancy`).
  - Режим Sandbox/Live: валидация на `STRIPE_MODE`, `sk_live_`, `https://` URLs; отхвърляне при mode mismatch.
  - Сверка & резервации: добавен `scripts/reconcile_orders.py` със запазване на резерва при async или недостъпен Stripe.
  - Заключвания: строг ред `Order` -> `Attempt` -> `Product` (asc); DB транзакциите са затворени по време на Stripe API calls.
  - Документация: обновени `README.md` (таблица на преходите, 4 Stripe CLI команди, sandbox SEPA демо, live инструкции).
- **Проверено / Резултати**:
  - Тестове: 211/211 теста в pytest преминават успешно; `ruff check` и `format` 100% чисти.
  - Непроверено: Реален Stripe Live акаунт (изисква боеви ключове `sk_live_...` и публичен HTTPS домейн).
- **Работещи команди**:
  - `cd services/commerce && uv run --locked pytest -q`
  - `cd services/commerce && uv run --locked ruff check . && uv run --locked ruff format --check .`
  - `cd services/commerce && uv run python scripts/reconcile_orders.py`
- **Засегнати файлове**: `app/config.py`, `models.py`, `service.py`, `reconcile_orders.py`, `README.md`, `tests/*`.
- **Следваща стъпка**: Потребителски преглед, git commit и push от потребителя.
