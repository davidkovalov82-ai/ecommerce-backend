# Session State

- **Текуща задача**: Етап К2 — POST /users/me/change-password и инвалидиране на сесии.
- **Реализирано**:
  - `POST /users/me/change-password` (204 No Content); грешна текуща парола -> 400.
  - Токен версиониране: колона `token_version` в `users` и Alembic миграция `09ee8360d8c2`.
  - При смяна на парола `token_version` се инкрементира атомарно; старите JWT губят достъп (401).
  - Валидация: 15–128 знака без strip; съвпадение със старата парола -> 422; липса на пароли в 422 отговора.
  - Защита от race conditions чрез `lock_by_id` (FOR UPDATE с `populate_existing=True`).
  - Поправки от ревю: отхвърляне на `\0` в display_name (422), възстановяване на предишен get_db_session override в К1.
- **Проверено / Резултати**:
  - `uv run --locked pytest`: 241 passed (100% успех, включително 7 теста в `test_profile_k2.py`).
  - `uv run --locked ruff check .` и `uv run --locked ruff format --check .`: 0 грешки.
- **Непроверено**: Интеграция с външен пощенски клиент за reset password (отделна задача).
- **Работещи команди**:
  - `cd services/commerce && uv run --locked pytest tests/accounts/test_profile_k2.py`
  - `cd services/commerce && uv run --locked pytest`
  - `cd services/commerce && uv run --locked ruff check . && uv run --locked ruff format --check .`
- **Засегнати файлове**: `app/modules/accounts/*`, `migrations/.../09ee8360d8c2_*.py`, `tests/accounts/*`.
- **Следваща стъпка**: Потребителски преглед и ръчен commit / push.
