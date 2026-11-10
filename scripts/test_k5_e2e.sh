#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:${PATH}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="commerce-k5-e2e"
COMPOSE_FILE="${ROOT_DIR}/compose.e2e.yaml"

cleanup() {
  echo "==> Cleaning up isolated K5 E2E stack (${PROJECT_NAME}) without touching working compose..."
  docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "${ROOT_DIR}"

echo "==> [1/3] Starting isolated K5 E2E infrastructure (Postgres, RabbitMQ, Mailpit, Stripe Mock, SMS Mock, Commerce, Payments, Notifications)..."
docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d --build --wait

echo "==> [2/3] Running all K5 E2E scenarios (offline Commerce, broker outage, concurrency, cancel/discrepancy, reconciliation, DLQ, cutover migration)..."
(
  cd "${ROOT_DIR}/services/commerce"
  uv run --locked python "${ROOT_DIR}/scripts/k5_e2e_verify.py"
)

echo "==> [3/3] K5 isolated E2E suite completed successfully."
