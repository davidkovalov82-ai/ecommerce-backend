# Session State

- **Текуща задача**: Преход към маркетплейс — Етап К1 (публикуване от потребителски профил).
- **Реализирано**:
  - `products.seller_id` с FK към `users.id` (ON DELETE RESTRICT) и Alembic миграция `9e7a669efa79`.
  - Прехвърляне на заварени продукти към деактивиран технически акаунт `legacy-catalog@internal.ecommerce.com`.
  - `POST /products`: всеки автентикиран потребител публикува стока; `seller_id` се задава от сървъра (в тялото -> 422).
  - `PATCH /products/{id}`: само собственик редактира (цена, наличност, активност); чужд/липсващ -> 404; admin няма служебен достъп.
  - `GET /users/me/products`: пагиниран личен списък на собствените активни и неактивни стоки.
- **Проверено / Резултати**:
  - `uv run --locked pytest`: 232 passed (100% успех, включително сценарии за Анна и Борис, пагинация, 422 и миграция).
  - `uv run --locked ruff check .` и `uv run --locked ruff format --check .`: 0 грешки.
- **Непроверено**: Интеграция с UI фронтенд клиент (тествано изцяло през API и БД).
- **Работещи команди**:
  - `cd services/commerce && uv run --locked pytest`
  - `cd services/commerce && uv run --locked ruff check . && uv run --locked ruff format --check .`
- **Засегнати файлове**: `app/modules/catalog/*`, `app/modules/accounts/*`, `migrations/*`, `tests/*`, `.agents/rules/learning.md`.
- **Следваща стъпка**: Потребителски преглед на локалните разлики и ръчен git commit / push.
