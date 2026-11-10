#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:${PATH}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="commerce-k6-e2e"
COMPOSE_FILE="${ROOT_DIR}/compose.e2e.yaml"

cleanup() {
  echo "==> Cleaning up isolated Users E2E stack (${PROJECT_NAME})..."
  docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "${ROOT_DIR}"

echo "==> [1/3] Starting isolated E2E infrastructure with real Users and Commerce..."
docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d --build --wait

echo "==> [2/3] Running reproducible Commerce -> Users E2E verification..."
(
  cd "${ROOT_DIR}/services/commerce"
  uv run --locked python "${ROOT_DIR}/scripts/users_e2e_verify.py"
)

echo "==> [3/3] Users isolated E2E suite completed successfully."

