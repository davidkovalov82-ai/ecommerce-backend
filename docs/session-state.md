# Session State

- **Текуща задача**: Етап К3: поръчка от няколко продавача, пратки, адрес за доставка и известия.
- **Реализирано**:
  - Модел/БД: миграция `e6f7a8b9c0d1`, таблица `shipments` (UNIQUE `order_id, seller_id`), `orders.fulfillment_status`, `order_items.seller_id`, адрес за доставка и частични outbox индекси.
  - Чекаут: задължителен валидиран `shipping_address` в `POST /orders`, създаване на пратка за всеки продавач, съхраняване на `seller_id` в позициите, защита от конфликт при еднакъв ключ и различен адрес (409).
  - Продавач: `GET /seller/shipments`, `GET /seller/shipments/{id}`, `POST /seller/shipments/{id}/ship` (изолация на артикули, 404 за чужди, 409 при неплатена/без адрес, идемпотентност).
  - Статуси: `fulfillment_status` преминава `unshipped -> partially_shipped -> shipped`, докато `order.status` остава `paid`.
  - Уведомяване: атомен outbox запис на `shipment.shipped` в транзакцията на изпращане; Mailpit известия с артикулите само на съответния продавач; устойчивост на SMTP срив.
- **Проверено / Резултати**:
  - `uv run --locked pytest -q`: 288 passed (100% успех, включително 17 нови теста за К3).
  - `uv run --locked ruff check .` и `uv run --locked ruff format --check .`: 0 грешки.
  - `alembic downgrade -1 && alembic upgrade head`: обратимост на миграцията потвърдена.
- **Непроверено**: At-least-once SMTP повторение при аварийно спиране на процеса между SMTP приемане и DB commit (документирано ограничение).
- **Работещи команди**:
  - `cd services/commerce && uv run --locked ruff check . && uv run --locked ruff format --check .`
  - `cd services/commerce && uv run --locked pytest tests/orders/test_k3*.py -v`
  - `cd services/commerce && uv run --locked pytest -q`
- **Засегнати файлове**: `app/modules/orders/*`, `app/main.py`, `migrations/.../e6f7a8b9c0d1_*.py`, `tests/orders/*`, `tests/conftest.py`.
- **Следваща стъпка**: Потребителски преглед и ръчен commit / push на завършения етап К3.
