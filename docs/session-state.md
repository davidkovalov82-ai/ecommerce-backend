# Session State

- **Текуща задача**: K6.4 — завършване на 6 микросервиса (Users, Catalog, Cart, Orders, Payments, Notifications) + Gateway.
- **Реализирано**:
  - `services/commerce` превърнат в `services/orders`: изчистени чужди таблици/модели/репозитории (миграция l6a7b8c9d0e1).
  - Nginx Reverse Proxy Gateway (`gateway/nginx.conf`, порт 8000/58000) с приоритетен маршрут `^~ /users/me/products` към Catalog.
  - Версионирана и идемпотентна очистка на количката при checkout с Outbox събитие `order.cart_clear` за надежден повтор при срив на Cart.
  - Обновени Compose файлове, CI workflows, Makefile (`make test-orders-e2e`), E2E тестове и TODO.md.
- **Проверено**:
  - `make test-orders-e2e` (`bash scripts/test_orders_e2e.sh`): пълен цикъл (регистрация -> продукт -> количка -> поръчка -> Stripe mock -> paid -> Mailpit уведомление, запазване на паралелна промяна в количката, идемпотентност, БД изолация).
  - Pytest във всички 6 сервиза: Orders (124 passed), Users (57 passed), Notifications (39 passed), Payments (34 passed), Cart (7 passed), Catalog (4 passed) — общо 265 passed, 0 failed.
  - Ruff lint & format: 100% чист код във всички 6 сервиза и E2E скриптовете.
- **Непроверено**: Натоварващи тестове и Kubernetes (извън обхвата на K6).
- **Работещи команди**: `make test-orders-e2e`, `make test-cart-e2e`, `make test-catalog-e2e`, `make test-users-e2e`.
- **Засегнати файлове**: `services/orders/*`, `services/cart/*`, `gateway/*`, `compose.yaml`, `compose.e2e.yaml`, `scripts/*`, `.github/workflows/ci.yml`.
- **Следваща стъпка**: Потребителят да прегледа и комитне промените.
