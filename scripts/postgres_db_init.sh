#!/bin/sh
# Инициализира работните PostgreSQL роли и БД и прехвърля собствеността без тръбопроводи (psql | psql),
# така че всяка грешка при генериране или изпълнение на SQL да връща ненулев exit code.
set -eu

export PGPASSWORD="${POSTGRES_PASSWORD}"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

COMMERCE_DB_NAME="${COMMERCE_DB_NAME:-commerce}"
NOTIF_DB_NAME="${NOTIF_DB_NAME:-notifications}"
PAYMENTS_DB_NAME="${PAYMENTS_DB_NAME:-payments}"
PAYMENTS_APP_USER="${PAYMENTS_APP_USER:-payments}"
PAYMENTS_APP_PASSWORD="${PAYMENTS_APP_PASSWORD:-payments}"

ensure_role() {
  role_name="$1"
  role_password="$2"
  psql -h "$DB_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -v role_name="$role_name" -At -o "$tmp_dir/check_role.txt" <<'SQL'
SELECT 1 FROM pg_roles WHERE rolname = :'role_name';
SQL
  if [ "$(tr -d '[:space:]' < "$tmp_dir/check_role.txt")" != "1" ]; then
    psql -h "$DB_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
      -v role_name="$role_name" -v role_password="$role_password" <<'SQL'
CREATE ROLE :"role_name" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'role_password';
SQL
  fi
}

ensure_database() {
  db_name="$1"
  owner_name="$2"
  psql -h "$DB_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -v db_name="$db_name" -At -o "$tmp_dir/check_db.txt" <<'SQL'
SELECT 1 FROM pg_database WHERE datname = :'db_name';
SQL
  if [ "$(tr -d '[:space:]' < "$tmp_dir/check_db.txt")" != "1" ]; then
    psql -h "$DB_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
      -v db_name="$db_name" -v owner_name="$owner_name" <<'SQL'
CREATE DATABASE :"db_name" OWNER :"owner_name";
SQL
  fi
}

transfer_ownership_and_lock_db() {
  db_name="$1"
  target_owner="$2"
  psql -h "$DB_HOST" -U "$POSTGRES_USER" -d "$db_name" -v ON_ERROR_STOP=1 \
    -v db_name="$db_name" -v target_owner="$target_owner" -At -o "$tmp_dir/transfer.sql" <<'SQL'
SELECT format('ALTER DATABASE %I OWNER TO %I;', :'db_name', :'target_owner')
WHERE (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :'db_name') <> :'target_owner';
SELECT format('ALTER SCHEMA public OWNER TO %I;', :'target_owner')
WHERE (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public') <> :'target_owner';
SELECT format('ALTER TABLE public.%I OWNER TO %I;', tablename, :'target_owner')
FROM pg_tables
WHERE schemaname = 'public' AND tableowner <> :'target_owner';
SELECT format('ALTER SEQUENCE public.%I OWNER TO %I;', sequencename, :'target_owner')
FROM pg_sequences
WHERE schemaname = 'public' AND sequenceowner <> :'target_owner';
SQL
  psql -h "$DB_HOST" -U "$POSTGRES_USER" -d "$db_name" -v ON_ERROR_STOP=1 -f "$tmp_dir/transfer.sql"
  psql -h "$DB_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -v db_name="$db_name" -v target_owner="$target_owner" <<'SQL'
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT, TEMP ON DATABASE :"db_name" TO :"target_owner";
SQL
}

ensure_role "$COMMERCE_APP_USER" "$COMMERCE_APP_PASSWORD"
ensure_role "$NOTIF_APP_USER" "$NOTIF_APP_PASSWORD"
ensure_role "$PAYMENTS_APP_USER" "$PAYMENTS_APP_PASSWORD"
ensure_database "$COMMERCE_DB_NAME" "$COMMERCE_APP_USER"
ensure_database "$NOTIF_DB_NAME" "$NOTIF_APP_USER"
ensure_database "$PAYMENTS_DB_NAME" "$PAYMENTS_APP_USER"
transfer_ownership_and_lock_db "$COMMERCE_DB_NAME" "$COMMERCE_APP_USER"
transfer_ownership_and_lock_db "$NOTIF_DB_NAME" "$NOTIF_APP_USER"
transfer_ownership_and_lock_db "$PAYMENTS_DB_NAME" "$PAYMENTS_APP_USER"
