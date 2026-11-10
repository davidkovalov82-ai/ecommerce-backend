# Session State

- **Текуща задача**: Етап K6.1 (част 2) — пренос на 8-те операции с акаунти в `services/users` (профил, пароли, модерация).
- **Реализирано**:
  - `GET /users/me` и `PATCH /users/me` (редакция на `display_name`, забрана за роля/статус).
  - `POST /users/me/change-password` (проверка на текуща парола, `token_version += 1`, отзоваване на сесии).
  - `POST /auth/password-reset/request` и `confirm` (rate limiting, защита от enumeration, еднократен reset токен).
  - `POST /users/{id}/block`, `/unblock`, `/close` (само за admin, одит лог, self-moderation и technical account защита).
  - Миграция `0002_password_reset_and_moderation_tables.py` (`password_reset_tokens`, `account_moderation_logs`).
  - `CurrentUserDep` и `AdminUserDep` с верификация на RS256 токена и прочитане на актуалния потребител от БД `users`.
  - 43 интеграционни теста в `services/users`: пълен цикъл на профил, пароли, модерация, токени и персистентност.
- **Проверено локално**: `services/users` ruff check/format (29 файла OK) + pytest (43 passed); миграция 0002 в `users` DB.
- **Непроверено**: пренос на съществуващи потребители от Commerce към Users; маршрутизация/Gateway към новите пътища.
- **Работещи команди**: `cd services/users && uv run --locked pytest -q`, `uv run --locked pytest tests/test_moderation.py::test_admin_block_and_unblock_cycle_and_token_invalidation`.
- **Засегнати файлове**: `services/users/{app/{api,config,dependencies,mailer,models,repository,schemas,service}.py,migrations/versions/0002*,tests/*}`, `TODO.md`, `docs/session-state.md`.
- **Следваща стъпка**: Скрипт за повторяем пренос на съществуващите потребители от Commerce към Users със запазване на ID и пароли.
