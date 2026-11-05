# Session State

- **Текуща задача**: Реализация на завършен модул за поръчки (`orders`) с транзакционен checkout и идемпотентност.
- **Реализирано**:
  - Alembic миграция `568b759ac3a0`: таблици `orders` и `order_items` с BigInteger суми, UNIQUE, CHECK и FK ограничения (RESTRICT/CASCADE).
  - Модели, схеми, repository, service и API: `POST /orders`, `GET /orders`, `GET /orders/{order_id}`.
  - Транзакционен протокол: `FOR NO KEY UPDATE` върху потребителя, заключване на продуктите по възходящ ID с `FOR UPDATE`, проверка на наличности, исторически snapshot, намаляване на склад, изчистване на количката и rollback при грешка.
  - Строга идемпотентност по `Idempotency-Key` (UUID) на ниво потребител.
  - Изчерпателни тестове в `test_orders.py` (19 теста) и `test_migration_sequence.py`, актуализирана документация в `README.md`.
- **Оставащи ограничения / Непроверено**:
  - Резервациите остават в `pending_payment` без автоматично изтичане (плащания и auto-expiration предстоят).
- **Работещи команди**:
  - `cd services/commerce && uv run --locked pytest -q`
  - `cd services/commerce && uv run --locked ruff check . && uv run --locked ruff format --check .`
- **Засегнати файлове**: `app/modules/orders/*`, `app/modules/cart/*`, `app/modules/catalog/*`, `app/modules/accounts/*`, `migrations/*`, `tests/*`, `README.md`.
- **Следваща стъпка**: Модул за плащания и управление на жизнения цикъл на поръчката (плащане, отказ, изтичане на резервация).
