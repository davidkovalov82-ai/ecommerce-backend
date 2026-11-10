.PHONY: help test-k4-e2e test-notifications-retry

help:
	@echo "E-Commerce Backend Makefile"
	@echo ""
	@echo "Налични цели:"
	@echo "  make test-k4-e2e              - Изолирана E2E проверка на К4 (PostgreSQL, RabbitMQ, Mailpit,"
	@echo "                                  Commerce + Notifications) без спиране на работния Compose."
	@echo "  make test-notifications-retry - Alias към make test-k4-e2e."

test-k4-e2e:
	@bash scripts/test_k4_e2e.sh

test-notifications-retry: test-k4-e2e
