#!/usr/bin/env bash
set -euo pipefail

export PATH="${HOME}/.local/bin:${PATH}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="commerce-k6-e2e"
COMPOSE_FILE="${ROOT_DIR}/compose.e2e.yaml"

KEYS_DIR="$(mktemp -d "${ROOT_DIR}/.tmp_keys_${PROJECT_NAME}_XXXXXX")"
bash "${ROOT_DIR}/scripts/prepare_e2e_keys.sh" "${KEYS_DIR}"
export USERS_E2E_JWT_PRIVATE_KEY_PATH="${KEYS_DIR}/jwt_private.pem"
export USERS_E2E_JWT_PUBLIC_KEY_PATH="${KEYS_DIR}/jwt_public.pem"

cleanup() {
  echo "==> Cleaning up isolated Users E2E stack (${PROJECT_NAME})..."
  docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "${KEYS_DIR:-}"
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
