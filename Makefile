.PHONY: help test-notifications-retry

TEST_ARGS ?= tests/orders/test_order_notifications.py -v

# По подразбиране показва наличните команди
help:
	@echo "E-Commerce Backend Makefile"
	@echo ""
	@echo "Налични цели:"
	@echo "  make test-notifications-retry  - Стартира PostgreSQL и Mailpit през Docker Compose,"
	@echo "                                   изчаква тяхната готовност и изпълнява теста за"
	@echo "                                   известия и retry в изолирана база данни (commerce_test)."

test-notifications-retry:
	@set -e; \
	echo "================================================================================"; \
	echo "  Стартиране на тест: Notifications Retry (PostgreSQL + Mailpit)"; \
	echo "================================================================================"; \
	echo ""; \
	echo "==> [Стъпка 1/3] Стартиране на тестови PostgreSQL и Mailpit през Docker Compose..."; \
	test -f services/commerce/.env || cp services/commerce/.env.example services/commerce/.env; \
	docker compose --env-file services/commerce/.env up -d db mailpit || { \
		echo "" >&2; \
		echo "❌ [ГРЕШКА] Стъпка 1 пропадна: Docker Compose не успя да стартира PostgreSQL / Mailpit." >&2; \
		exit 1; \
	}; \
	echo ""; \
	echo "==> [Стъпка 2/3] Изчакване на готовност на PostgreSQL (5432) и Mailpit (1025, 8025)..."; \
	READY=0; \
	for i in $$(seq 1 30); do \
		if docker compose --env-file services/commerce/.env exec -T db pg_isready -U commerce -d commerce >/dev/null 2>&1 && \
		   curl -s -f http://127.0.0.1:8025/api/v1/messages >/dev/null 2>&1; then \
			READY=1; \
			break; \
		fi; \
		sleep 1; \
	done; \
	if [ "$$READY" -ne 1 ]; then \
		echo "" >&2; \
		echo "❌ [ГРЕШКА] Стъпка 2 пропадна: PostgreSQL или Mailpit не станаха готови в рамките на 30 секунди." >&2; \
		exit 1; \
	fi; \
	echo "Контейнерите са здрави и готови за тестове."; \
	echo ""; \
	echo "==> [Стъпка 3/3] Стартиране на теста за notifications retry в изолирана база (commerce_test)..."; \
	cd services/commerce && uv run --locked pytest $(TEST_ARGS) || { \
		EXIT_CODE=$$?; \
		echo "" >&2; \
		echo "❌ [ГРЕШКА] Стъпка 3 пропадна: Тестът завърши с грешка (код $$EXIT_CODE). Вижте подробностите за неуспешния assert по-горе." >&2; \
		exit $$EXIT_CODE; \
	}; \
	echo ""; \
	echo "================================================================================"; \
	echo "✅ Всички тестове за Notifications Retry преминаха успешно!"; \
	echo "================================================================================"

