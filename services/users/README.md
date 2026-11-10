# Users Service

Микросервис за регистрация, автентикация и управление на потребителски акаунти в платформата за електронна търговия.

## Архитектура
Проектът следва стриктна трислойна архитектура:
- **API слой (`app/api.py`, `app/schemas.py`)**: Обработва HTTP заявките, Pydantic валидацията и трансформацията на бизнес грешки към стандартизирани HTTP статуси.
- **Service слой (`app/service.py`)**: Изпълнява бизнес сценариите (регистрация с хеширане на пароли, автентикация със защита от timing attack, проверка на блокировки) и определя границите на SQLAlchemy транзакциите (`commit`/`rollback`).
- **Repository слой (`app/repository.py`)**: Изпълнява SQL заявките към таблицата `users` с изолирана сесия.

## Асиметрични JWT токени (RS256)
- Users издава JWT токени, подписани с частен RSA 2048 ключ (`RS256`).
- Публичният ключ се предоставя през `GET /auth/public-key` за локална проверка от външни микросервизи (Commerce, Orders и др.) без необходимост от обратна връзка при всяка заявка.
- Задължителни claims: `sub` (потребителско ID), `role`, `email`, `token_version`, `iss` (`ecommerce-users`), `aud` (`ecommerce`), `iat`, `exp` (15 минути).
- Частният ключ **не се качва в Git**.

## Локална настройка и ключове

1. Генериране на двойка RSA ключове:
   ```bash
   cd services/users
   uv run python scripts/generate_keys.py
   ```
   Това създава локални `jwt_private.pem` и `jwt_public.pem` (добавени в `.gitignore`).

2. Настройка на `.env`:
   ```bash
   cp .env.example .env
   ```

3. Инсталиране на зависимостите:
   ```bash
   uv sync --locked --dev
   ```

4. Прилагане на миграции:
   ```bash
   uv run --locked alembic upgrade head
   ```

5. Стартиране на локалния сървър за разработка:
   ```bash
   uv run --locked uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
   ```

## Тестове и линтинг

- Изпълнение на тестовия пакет (използва изолирана база `users_test`):
  ```bash
  uv run --locked pytest -q
  ```
- Проверка на код и форматиране с Ruff:
  ```bash
  uv run --locked ruff check .
  uv run --locked ruff format --check .
  ```

## Стартиране с Docker Compose
```bash
docker compose up -d users
```
Сервисът ще бъде достъпен на порт `8002`, със здравен статус на `GET http://localhost:8002/health`.

