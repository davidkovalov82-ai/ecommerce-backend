# Session State

- **Текуща задача**: Корекции по устойчивост и тестове в Catalog и Orders (забележки към 4c4905f).
- **Реализирано**:
  - Orders: `_publish_stock_release_immediate` извиква Catalog само при запазено pending `order.stock_release` и статус `cancelled`/`expired`; при открит `paid` статус в `create_stripe_checkout_session` резервът не се освобождава.
  - Catalog: тестовият вътрешен токен ползва `settings.internal_auth_token` като единен източник (синхронизиран с CI env); запазени проверки за празен, липсващ и невалиден токен.
  - Тестове Orders: пренаписан `test_cancel_order_rollback_does_not_release_stock` (успешен първи commit, грешка на commit за cancelled/outbox, потвърден rollback); добавен тест за промяна към `paid` между фазите.
- **Проверено**:
  - Unit suites: Orders (128), Catalog (11) — общо 139 passed, 0 failed.
  - E2E suites: `make test-orders-e2e`, `make test-catalog-e2e`, `make test-cart-e2e`, `make test-users-e2e` (чист стенд).
  - Ruff check & format: 100% чист код в Catalog и Orders.
- **Непроверено**: Продукционна мулти-нод среда (извън обхвата).
- **Работещи команди**: `make test-orders-e2e`, `make test-catalog-e2e`, `uv run --locked pytest`.
- **Засегнати файлове**: `services/catalog/tests/*`, `services/orders/app/*`, `services/orders/tests/*`.
- **Следваща стъпка**: Преглед и commit от потребителя (`git add` / `git commit`).
