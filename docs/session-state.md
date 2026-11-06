# Session State

- **Текуща задача**: Поправка на Stripe Checkout интеграция и премахване на симулатора в `feat/payments`.
- **Реализирано**:
  - CI & Docker: `STRIPE_SECRET_KEY` и `STRIPE_WEBHOOK_SECRET` в `compose.yaml`; премахнати дублиращи настройки в `config.py`.
  - Премахнат симулатор: изтрит `POST /orders/{id}/payments`, `PaymentSimulator` и схеми; запазена история на миграциите.
  - Състезания: `initiated` и `cancelling` не освобождават stock; заключване `FOR UPDATE`; закъснял отговор не връща в `open`.
  - Webhook: пълна проверка на подпис, sandbox, `attempt_id`, сума и валута; дедупликация на събития без 500 грешки.
  - Съвместимост: премахнат остарял `payment_method_types` за Stripe API `2025-09-30.clover` с динамични методи.
  - Документация & OpenAPI: обновени `test_openapi.py` и `README.md`.
- **Проверено / Резултати**:
  - Локални тестове: 200/200 теста в pytest преминават успешно; `ruff check` и `format` 100% чисти.
  - Ръчен Sandbox тест: успешно плащане и обработен webhook `evt_1UYmjwRLtNVNvv968oh2H9Ra` -> поръчка 2 е `paid`.
  - Непроверено: GitHub CI (очаква push от потребителя).
- **Работещи команди**:
  - `cd services/commerce && uv run --locked pytest -q`
  - `cd services/commerce && uv run --locked ruff check . && uv run --locked ruff format --check .`
- **Засегнати файлове**: `compose.yaml`, `README.md`, `services/commerce/app/*`, `services/commerce/tests/*`.
- **Следваща стъпка**: Потребителски преглед, git commit и push от потребителя.
