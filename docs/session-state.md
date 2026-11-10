# Session State

- **Текуща задача**: Етап K6.1 — възпроизводим E2E Commerce → Users от чист checkout, изолация на ключове и CI.
- **Реализирано**:
  - `scripts/prepare_e2e_keys.sh`: обща подготовка на временна 2048-битова RSA-двойка (Users: двата ключа, Commerce: само публичния; ключовете на разработчика не се докосват; почистване при изход).
  - `compose.e2e.yaml`: контейнер `commerce-migrations-e2e` за предварителна подготовка на БД; `users-e2e` мигрира предишните акаунти и синхронизира брояча преди пускане на регистрацията.
  - `scripts/users_e2e_verify.py`: усилена проверка (Users ред с ID от API, без съвпадение по email или ID в `commerce.users`, `GET /cart` връща празен `items`).
  - `.github/workflows/ci.yml`: добавена стъпка `users-e2e-checks` с `make test-users-e2e`.
- **Проверено локално**:
  - `make test-users-e2e`: успешно на чист стенд (ID=2, без колизия, празна количка);
  - `make test-k4-e2e`: успешно (всички 5 сценария + DLQ);
  - `make test-k5-e2e`: успешно (всички K5 сценарии + cutover);
  - `ruff check .` и `ruff format --check .` в `services/commerce` (118) и `services/users` (33) чисти.
- **Непроверено**: финално архивиране на старите потребителски модели в Commerce.
- **Работещи команди**: `make test-users-e2e`, `make test-k4-e2e`, `make test-k5-e2e`.
- **Засегнати файлове**: `compose.e2e.yaml`, `scripts/{prepare_e2e_keys.sh,test_*_e2e.sh,users_e2e_verify.py}`, `.github/workflows/ci.yml`, `docs/session-state.md`.
- **Следваща стъпка**: Пълно затваряне на K6.1 след преглед на CI.
