.PHONY: help test-k4-e2e test-k5-e2e test-notifications-retry test-users-e2e test-catalog-e2e test-cart-e2e test-orders-e2e

help:
	@echo "E-Commerce Backend Makefile"
	@echo ""
	@echo "Налични цели:"
	@echo "  make test-orders-e2e          - Изолирана E2E проверка на чист стенд за K6.4 (всички 6 микросервиса + Gateway)."
	@echo "  make test-cart-e2e            - Изолирана E2E проверка на чист стенд за K6.3 (Cart lifecycle/checkout)."
	@echo "  make test-catalog-e2e         - Изолирана E2E проверка на чист стенд за K6.2 (Catalog stock/orders)."
	@echo "  make test-users-e2e           - Изолирана E2E проверка на реална връзка Orders -> Users (201 -> 200 -> 200)."
	@echo "  make test-k4-e2e              - Изолирана E2E проверка на К4 (PostgreSQL, RabbitMQ, Mailpit,"
	@echo "                                  Orders + Payments + Notifications) без спиране на работния Compose."
	@echo "  make test-k5-e2e              - Изолирана E2E проверка на К5 (независим Payments, срив на Orders,"
	@echo "                                  загубен Stripe отговор, отложено/късно плащане, миграция)."
	@echo "  make test-notifications-retry - Alias към make test-k4-e2e."

test-orders-e2e:
	@bash scripts/test_orders_e2e.sh

test-cart-e2e:
	@bash scripts/test_cart_e2e.sh

test-catalog-e2e:
	@bash scripts/test_catalog_e2e.sh

test-users-e2e:
	@bash scripts/test_users_e2e.sh

test-k4-e2e:
	@bash scripts/test_k4_e2e.sh

test-k5-e2e:
	@bash scripts/test_k5_e2e.sh

test-notifications-retry: test-k4-e2e
