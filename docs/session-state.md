# Session State

- **Текуща задача**: Етап K6.1 — пробен пренос (репетиция) на акаунти от Commerce към Users на изолирани бази.
- **Реализирано**:
  - `services/users/scripts/migrate_from_commerce.py`: CLI команда и функция за пренос с режими `--dry-run` и `--apply`.
  - Пренася `users`, `account_moderation_logs` и `password_reset_tokens` с първоначалните ID-та, хешове, статус, token_version и срокове.
  - Без отпечатване на хешове/токени; `UserRepository.sync_sequences()` предотвратява PK колизии при нови регистрации.
  - Идемпотентност при повторен пренос; чист rollback при конфликт в ID или email без оставяне на частични данни.
  - `services/users/tests/test_migrate_from_commerce.py`: 4 интеграционни теста на PostgreSQL за вход, модерация, токени, конфликти и sequence.
- **Проверено локално**:
  - `services/users`: `uv run --locked pytest tests/test_migrate_from_commerce.py` (4 passed);
  - `services/users`: `uv run --locked pytest -q` (48 passed, 1 warning);
  - `services/users`: `uv run --locked ruff check .` и `uv run --locked ruff format --check .` (32 files);
  - `services/commerce` и `services/payments`: ruff check & format --check (115 и 32 files).
- **Непроверено**: превключване на Commerce API или авторизация към Users; финален cutover (това е само репетиция на преноса).
- **Работещи команди**: `cd services/users && uv run --locked pytest tests/test_migrate_from_commerce.py`.
- **Засегнати файлове**: `services/users/app/repository.py`, `services/users/scripts/migrate_from_commerce.py`, `services/users/tests/test_migrate_from_commerce.py`, `services/users/README.md`, `docs/session-state.md`.
- **Следваща стъпка**: Верификация на RS256 JWT токени от Users в Commerce и превключване на маршрутите за акаунти.
