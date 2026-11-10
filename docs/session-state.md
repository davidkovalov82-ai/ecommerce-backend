# Session State

- **Текуща задача**: Етап K6.1 (част 1) — отделен независим микросервис `services/users` с регистрация и вход.
- **Реализирано**:
  - `services/users`: FastAPI, синхронен SQLAlchemy 2.0, PostgreSQL, Alembic, uv, pytest, Ruff. Архитектура API -> Service -> Repository.
  - Изолирана PostgreSQL роля и база `users` (`scripts/postgres_db_init.sh`, `compose.yaml`). Порт `8002`.
  - Модел `User` и миграция `0001_create_users_table.py` (id, email, password_hash, role='customer', status='active', token_version=0).
  - Регистрация (`POST /users`), вход (`POST /auth/login`), здраве (`GET /health`) и публичен ключ (`GET /auth/public-key`).
  - Асиметричен JWT (RS256, 15m, `iss="ecommerce-users"`, `aud="ecommerce"`, `sub`, `role`, `email`, `token_version`). Частен ключ извън Git; скрипт `scripts/generate_keys.py`.
  - Dockerfile, compose конфигурация, .env.example, CI job `users-checks` в `.github/workflows/ci.yml`.
  - 26 интеграционни теста (PostgreSQL): миграции, регистрация (дубликати, правила, роли), вход (грешни данни, блокировки), токени, персистентност.
- **Проверено локално**: `services/users` ruff check/format (23 файла OK) + pytest (26 passed); реална миграция към `users` DB; Docker compose config; извличане на /health и /auth/public-key.
- **Непроверено**: пълен Docker Compose build в чист Docker daemon; превключване на Commerce към Users (отложено за следващи части на K6.1).
- **Работещи команди**: `cd services/users && uv run --locked pytest -q`, `cd services/users && uv run --locked ruff check . && uv run --locked ruff format --check .`, `docker compose up -d users`.
- **Засегнати файлове**: `services/users/*`, `compose.yaml`, `scripts/postgres_db_init.sh`, `.github/workflows/ci.yml`, `README.md`, `TODO.md`, `docs/session-state.md`.
- **Следваща стъпка**: Преглед и пускане на промените от потребителя, преминаване към следващата фаза на K6.1.
