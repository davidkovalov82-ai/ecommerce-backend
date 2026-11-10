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

- Изпълнение на пълния тестов пакет (използва изолирана база `users_test` и Mailpit):
  ```bash
  uv run --locked pytest -q
  ```
- Изпълнение конкретно на интеграционната проверка за възстановяване на парола през Mailpit:
  ```bash
  uv run --locked pytest tests/test_password_reset_mailpit.py
  ```
- Проверка на код и форматиране с Ruff:
  ```bash
  uv run --locked ruff check .
  uv run --locked ruff format --check .
  ```

## SMTP и Mailpit
За изпращане на имейли за възстановяване на парола Users използва SMTP:
- По подразбиране в Docker Compose: `SMTP_HOST=mailpit`, `SMTP_PORT=1025`, `SMTP_FROM=noreply@example.com`.
- Локално за разработка/тестове се свързва към Mailpit на `localhost:1025`, а получените писма могат да се преглеждат в Web UI на `http://localhost:8025`.

## Пренос на акаунти от Commerce (K6.1)
Скриптът `scripts/migrate_from_commerce.py` реализира безопасен и идемпотентен пренос на потребители, одит логове за модерация и токени за ресет от Commerce към Users.

### Условия за финален пренос (Cutover):
1. **Промените по акаунти в Commerce са спрени**: всякакви промени по потребители (регистрация, вход, смяна/възстановяване на парола, административна модерация) в Commerce трябва да бъдат временно преустановени.
2. **Записът в Users все още не е отворен**: публичният достъп за запис и промени в новия Users сервис се открива едва след успешно приключване на финалния пренос.
3. **Изолирани бази данни**: всички предварителни репетиции, проверки и тестове се изпълняват **само върху напълно изолирани тестови бази данни**.

- **Dry-run симулация** (по подразбиране, без промени по целевата база):
  ```bash
  uv run --locked python scripts/migrate_from_commerce.py --commerce-url "$COMMERCE_DATABASE_URL" --dry-run
  ```
- **Прилагане на преноса** (транзакционен запис със синхронизация на PostgreSQL sequences):
  ```bash
  uv run --locked python scripts/migrate_from_commerce.py --commerce-url "$COMMERCE_DATABASE_URL" --apply
  ```
- **Интеграционен тест на преноса** (на изолирани PostgreSQL бази):
  ```bash
  uv run --locked pytest tests/test_migrate_from_commerce.py
  ```

## Стартиране с Docker Compose
```bash
docker compose up -d users
```
Сервисът ще бъде достъпен на порт `8002`, със здравен статус на `GET http://localhost:8002/health`.

