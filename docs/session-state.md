# Session State

- **Текуща задача**: Корекции на устойчивостта и детайлите в Catalog, Cart и Orders (K6.2–K6.4).
- **Реализирано**:
  - Orders: `_restore_order_stock` записва статус и pending outbox в една транзакция; Catalog release се вика само след commit; защитен rollback.
  - Catalog: терминален запис за release (`order_stock_releases`) предотвратява късен reserve; премахнат дублиран advisory lock.
  - Catalog consumer: разграничени временни DB грешки (requeue+delay) от невалидни събития (nack); строга валидация на `occurred_at`.
  - CI: Cart и Catalog стартират `init_db` през `uv run --locked python -m scripts.init_db`.
- **Проверено**:
  - Unit suites: Orders (127), Catalog (11), Cart (9), Users (57), Payments (34), Notifications (39) — общо 277 passed.
  - E2E suites: `make test-catalog-e2e`, `make test-cart-e2e`, `make test-orders-e2e`, `make test-users-e2e` (чист стенд).
  - Ruff check & format: 100% чист във всички 6 сервиза.
- **Непроверено**: Продукционна мулти-нод среда (извън обхвата).
- **Работещи команди**: `make test-orders-e2e`, `make test-cart-e2e`, `make test-catalog-e2e`, `make test-users-e2e`.
- **Засегнати файлове**: `services/catalog/*`, `services/orders/*`, `services/cart/scripts/*`, `.github/workflows/ci.yml`.
- **Следваща стъпка**: Преглед и commit от потребителя (`git add` / `git commit`).
