#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:${PATH}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="commerce-k6-3-clean"
COMPOSE_FILE="${ROOT_DIR}/compose.e2e.yaml"

KEYS_DIR="$(mktemp -d "${ROOT_DIR}/.tmp_keys_${PROJECT_NAME}_XXXXXX")"
bash "${ROOT_DIR}/scripts/prepare_e2e_keys.sh" "${KEYS_DIR}"
export USERS_E2E_JWT_PRIVATE_KEY_PATH="${KEYS_DIR}/jwt_private.pem"
export USERS_E2E_JWT_PUBLIC_KEY_PATH="${KEYS_DIR}/jwt_public.pem"

cleanup() {
  local exit_code=$?
  if [ "${exit_code}" -ne 0 ]; then
    echo "==> Failure detected (exit code: ${exit_code}). Dumping service logs..."
    docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" logs cart-e2e || true
    docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" logs catalog-e2e || true
    docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" logs commerce-api-e2e || true
  fi
  echo "==> Cleaning up isolated clean stand stack (${PROJECT_NAME})..."
  docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "${KEYS_DIR:-}"
}
trap cleanup EXIT

cd "${ROOT_DIR}"

echo "==> [1/2] Starting isolated clean stand infrastructure (Cart, Catalog, Commerce, Users)..."
docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d --build --wait

echo "==> [2/2] Running K6.3 standalone Cart E2E verification..."
(
  cd "${ROOT_DIR}/services/commerce"
  export COMPOSE_PROJECT_NAME="${PROJECT_NAME}"
  export COMPOSE_FILE="${COMPOSE_FILE}"
  uv run --locked python "${ROOT_DIR}/scripts/cart_e2e_verify.py"
)

echo "==> [DONE] K6.3 standalone Cart test completed successfully on clean stand."
