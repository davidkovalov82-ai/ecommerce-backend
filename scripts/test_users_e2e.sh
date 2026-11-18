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
  local exit_code=$?
  if [ "${exit_code}" -ne 0 ]; then
    echo "==> Failure detected (exit code: ${exit_code}). Dumping consumer and outbox logs..."
    docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" logs users-outbox-e2e || true
    docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" logs catalog-user-consumer-e2e || true
  fi
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
  cd "${ROOT_DIR}/services/orders"
  export COMPOSE_PROJECT_NAME="${PROJECT_NAME}"
  export COMPOSE_FILE="${COMPOSE_FILE}"
  uv run --locked python "${ROOT_DIR}/scripts/users_e2e_verify.py"
)

echo "==> [2.1/3] Verifying Users restart preserves profile updates..."
# 1. Обновяваме профила на съществуващ потребител директно в Users БД
TEST_USER_EMAIL=$(docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" exec -T db-e2e psql -U users_app -d users -tAc \
  "SELECT email FROM users ORDER BY id ASC LIMIT 1;")

if [ -n "${TEST_USER_EMAIL}" ]; then
  docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" exec -T db-e2e psql -U users_app -d users \
    -c "UPDATE users SET display_name = 'Preserved Seller Profile' WHERE email = '${TEST_USER_EMAIL}';"

  # 2. Рестартираме контейнера users-e2e
  docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" restart users-e2e

  # 3. Изчакваме готовността на users-e2e
  echo "    Waiting for Users service to become healthy after restart..."
  until [ "$(docker inspect --format='{{json .State.Health.Status}}' users-api-e2e 2>/dev/null)" = '"healthy"' ]; do
    sleep 1
  done

  # 4. Проверяваме, че промяната в профила е запазена
  UPDATED_NAME=$(docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" exec -T db-e2e psql -U users_app -d users -tAc \
    "SELECT display_name FROM users WHERE email = '${TEST_USER_EMAIL}';")

  if [ "${UPDATED_NAME}" != "Preserved Seller Profile" ]; then
    echo "ERROR: Profile change was overwritten after Users restart! Got: '${UPDATED_NAME}'"
    exit 1
  fi
  echo "==> Profile preserved across Users restart ('${UPDATED_NAME}')."
fi

echo "==> [3/3] Users isolated E2E suite completed successfully."
