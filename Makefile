.PHONY: help test-k4-e2e test-k5-e2e test-notifications-retry test-users-e2e

help:
	@echo "E-Commerce Backend Makefile"
	@echo ""
	@echo "Налични цели:"
	@echo "  make test-users-e2e           - Изолирана E2E проверка на реална връзка Commerce -> Users (201 -> 200 -> 200)."
	@echo "  make test-k4-e2e              - Изолирана E2E проверка на К4 (PostgreSQL, RabbitMQ, Mailpit,"
	@echo "                                  Commerce + Payments + Notifications) без спиране на работния Compose."
	@echo "  make test-k5-e2e              - Изолирана E2E проверка на К5 (независим Payments, срив на Commerce,"
	@echo "                                  загубен Stripe отговор, отложено/късно плащане, миграция)."
	@echo "  make test-notifications-retry - Alias към make test-k4-e2e."

test-users-e2e:
	@bash scripts/test_users_e2e.sh

test-k4-e2e:
	@bash scripts/test_k4_e2e.sh

test-k5-e2e:
	@bash scripts/test_k5_e2e.sh

test-notifications-retry: test-k4-e2e
