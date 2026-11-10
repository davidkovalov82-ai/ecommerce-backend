# Session State

- **Текуща задача**: K6.1 — валидация на статус на продавачите през Users при `POST /orders`, изолация на миграцията в Compose.
- **Реализирано**:
  - `compose.e2e.yaml`: отделна еднократна задача `users-accounts-migration-e2e` (`restart: "no"`); рестартът на Users не повтаря миграцията и запазва промените в профилите.
  - `OrderService.create_order`: статусът на продавачите се проверява единствено през `UsersClient.get_user_status`; неактивен/блокиран/непознат връща 409 `CART_PRODUCT_UNAVAILABLE`; недостъпен Users връща 503; при отказ няма създадена поръчка, остатъците и количката са непроменени; запазена идемпотентност.
  - `scripts/users_e2e_verify.py` & `test_users_e2e.sh`: реални продавач и купувач, отказ при невалиден продавач, успешна поръчка при валиден, запазване на профил при рестарт на Users контейнера.
- **Проверено локално**:
  - `make test-users-e2e`: успешно (отказ 409, успех 201, намален остатък 10->8, чиста количка, запазен профил след рестарт);
  - `make test-k4-e2e` & `make test-k5-e2e`: успешно;
  - `commerce` pytest: 310 passed; `users` pytest: 57 passed; Ruff (check/format) чист в двата сервиза.
- **Непроверено**: няма.
- **Работещи команди**: `make test-users-e2e`, `make test-k4-e2e`, `make test-k5-e2e`, `uv run --locked pytest -q`.
- **Засегнати файлове**: `compose.e2e.yaml`, `scripts/{test_users_e2e.sh,users_e2e_verify.py}`, `services/commerce/app/modules/orders/{api.py,dependencies.py,service.py}`, `services/commerce/tests/integration/test_users_integration.py`, `docs/session-state.md`.
- **Следваща стъпка**: Преглед от потребителя и commit.
