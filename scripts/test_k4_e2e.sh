#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="commerce-k4-e2e"
COMPOSE_FILE="${ROOT_DIR}/compose.e2e.yaml"

cleanup() {
  echo "==> Cleaning up isolated E2E stack (${PROJECT_NAME}) without touching working compose..."
  docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "${ROOT_DIR}"

echo "==> [1/3] Starting isolated E2E infrastructure (Postgres, RabbitMQ, Mailpit, Commerce, Notifications)..."
docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d --build --wait

echo "==> [2/3] Running all 5 K4 E2E scenarios + isolation/DLQ checks..."
(
  cd "${ROOT_DIR}/services/notifications"
  uv run --locked python "${ROOT_DIR}/scripts/k4_e2e_verify.py"
)

echo "==> [3/3] K4 isolated E2E suite completed successfully."

