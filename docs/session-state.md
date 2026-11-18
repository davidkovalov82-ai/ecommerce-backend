# Session State

- **Текуща задача**: Завършване на интеграцията K6.2–K6.4 след отделянето на Catalog, Cart и Orders.
- **Реализирано**:
  - Catalog & Cart: валидация на статус, роля и token_version през Users; fail-closed защита; празен вътрешен токен не заобикаля автентикацията.
  - Catalog consumer: валидация на схемата, атомарна дедупликация и защита от презаписване с по-стари събития.
  - Catalog наличности: advisory lock за паралелни reserve/release; 409 при конфликтен състав за същия order_id; валидация за празен/дублиран състав.
  - Orders: грешка след commit не освобождава наличност; release при отказ се записва в Outbox (`order.stock_release`) за фонов retry.
  - Cart: монотонни версии през sequence `cart_item_version_seq`; изчистване само по точни версии; advisory lock и дедупликация по order_id.
  - Премахнати остатъчни чужди домейни от Orders; обновени Compose и CI за директни Users проверки.
- **Проверено**:
  - `make test-catalog-e2e`, `make test-cart-e2e`, `make test-orders-e2e`, `make test-users-e2e` (всички 4 E2E преминават на чист стенд).
  - Unit тестове: Users (57), Catalog (8), Cart (9), Orders (126), Payments (34), Notifications (39) — общо 273 passed, 0 failed.
  - Ruff lint & format: 100% чист код във всички 6 сервиза и скриптове.
- **Непроверено**: Натоварващо тестване в реална мулти-нод среда (извън обхвата).
- **Работещи команди**: `make test-orders-e2e`, `make test-cart-e2e`, `make test-catalog-e2e`, `make test-users-e2e`.
- **Засегнати файлове**: `services/catalog/*`, `services/cart/*`, `services/orders/*`, `compose*.yaml`, `scripts/*`.
- **Следваща стъпка**: Готово за commit и push от потребителя.
