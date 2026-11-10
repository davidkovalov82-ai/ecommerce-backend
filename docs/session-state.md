# Session State

- **Текуща задача**: Етап K6.1 — доказване на реална връзка Commerce → Users в Docker без подмени.
- **Реализирано**:
  - `compose.e2e.yaml` и `compose.yaml`: добавена услуга `users-e2e` със собствена БД, Alembic миграции, RSA ключове (`jwt_public.pem:ro`), `/health` проверка и зависимост на Commerce.
  - `scripts/users_e2e_verify.py` и `scripts/test_users_e2e.sh`: сценарий 201 (`POST /users`) → 200 (`POST /auth/login`) → 200 (`GET /cart`), с директна проверка в PostgreSQL (акаунтът е в `users.users` и отсъства в `commerce.users`).
  - `Makefile`: добавена цел `test-users-e2e`.
- **Проверено локално**:
  - `make test-users-e2e` (201 -> 200 -> 200, DB проверка: успешно);
  - `make test-k4-e2e` (5/5 сценария успешни); `make test-k5-e2e` (всички K5 сценарии успешни);
  - `services/commerce`: `ruff check .`, `ruff format --check .` (All checks passed, 118 files), `pytest -q` (309 passed);
  - `services/users`: `ruff check .`, `ruff format --check .` (All checks passed, 33 files), `pytest -q` (57 passed).
- **Непроверено**: окончателно изчистване на историческата таблица `commerce.users` и пълен завършек на K6.1.
- **Работещи команди**: `make test-users-e2e`, `make test-k4-e2e`, `make test-k5-e2e`.
- **Засегнати файлове**: `Makefile`, `compose.e2e.yaml`, `compose.yaml`, `scripts/{test_users_e2e.sh,users_e2e_verify.py}`, `services/commerce/app/config.py`, `services/users/app/main.py`, `docs/session-state.md`.
- **Следваща стъпка**: Довършване на останалите изисквания за пълно затваряне на K6.1 (миграция на данни и деактивация на стари модели).
