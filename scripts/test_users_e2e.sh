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

echo "==> [2.1/3] Verifying Users restart preserves profile updates and does not rerun migration..."
# 1. Обновяваме профила на пренесения потребител директно в Users БД
docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" exec -T db-e2e psql -U users_app -d users \
  -c "UPDATE users SET display_name = 'Preserved Legacy Seller Profile' WHERE email = 'legacy-catalog@internal.ecommerce.com';"

# 2. Рестартираме контейнера users-e2e
docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" restart users-e2e

# 3. Изчакваме готовността на users-e2e
echo "    Waiting for Users service to become healthy after restart..."
until [ "$(docker inspect --format='{{json .State.Health.Status}}' users-api-e2e 2>/dev/null)" = '"healthy"' ]; do
  sleep 1
done

# 4. Проверяваме, че еднократната задача за миграция не е рестартирана
MIGRATION_STATUS=$(docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" ps -a --format '{{.Status}}' users-accounts-migration-e2e)
echo "    One-off accounts migration task status: ${MIGRATION_STATUS}"

# 5. Проверяваме, че промяната в профила е запазена и не е презаписана от миграция
UPDATED_NAME=$(docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" exec -T db-e2e psql -U users_app -d users -tAc \
  "SELECT display_name FROM users WHERE email = 'legacy-catalog@internal.ecommerce.com';")

if [ "${UPDATED_NAME}" != "Preserved Legacy Seller Profile" ]; then
  echo "ERROR: Migrated profile change was overwritten after Users restart! Got: '${UPDATED_NAME}'"
  exit 1
fi
echo "==> Migrated profile preserved across Users restart ('${UPDATED_NAME}')."

echo "==> [3/3] Users isolated E2E suite completed successfully."
