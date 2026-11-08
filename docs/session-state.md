# Session State

- **Текуща задача**: Завършване на Етап К2: възстановяване на парола, модерация на акаунти и поведение на маркетплейса при модерирани продавачи.
- **Реализирано**:
  - Възстановяване на парола (`POST /auth/password-reset/request`, `POST /auth/password-reset/confirm`): 202 отговор без изтичане на данни при грешки/SMTP срив, SHA-256 хеш на токен в DB, 15 мин валидност, Mailpit интеграция, rate limiting, атомарно инвалидиране на сесии (`token_version += 1`).
  - Единен ред на блокировки: винаги първо `users` (lock_by_id), след това `password_reset_tokens`. Погасяване на линкове при обикновена смяна на парола.
  - Модерация на акаунти: състояния `active`, `blocked`, `closed`, Alembic миграция `f3c8a91b4d2e`, одит журнал `account_moderation_logs`.
  - Маркетплейс: скриване на стоки на блокирани/закрити продавачи в каталог и детайли (404), `PUT /cart/items/{id}` дава 404, отхвърляне на checkout (409) без промяна на склад и количка; запазване на историята на платени поръчки.
  - Поправени тестове: реални SHA-256 хешове за изтекли токени, премахнати дубликати, тестове за конкурентност и SMTP устойчивост.
- **Проверено / Резултати**:
  - `uv run --locked pytest -q`: 262 passed, 0 failed, 0 skipped (100% успех на пълния suite).
  - `uv run --locked ruff check .` и `uv run --locked ruff format --check .`: 0 грешки (101 файла форматирани).
  - Alembic: успешно тестван upgrade head и downgrade -1.
- **Непроверено**: Няма (всички изисквания на етап К2 са напълно проверени).
- **Работещи команди**:
  - `cd services/commerce && uv run --locked ruff check . && uv run --locked ruff format --check .`
  - `cd services/commerce && uv run --locked alembic upgrade head`
  - `cd services/commerce && uv run --locked pytest -q`
- **Засегнати файлове**: `app/modules/accounts/*`, `app/modules/catalog/*`, `app/modules/cart/*`, `app/modules/orders/*`, `migrations/.../f3c8a91b4d2e_*.py`, `tests/*`.
- **Следваща стъпка**: Потребителски преглед и ръчен commit / push.
