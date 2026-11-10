"""Интеграционни тестове за пренос на акаунти от Commerce към Users на изолирани PostgreSQL бази."""

import datetime
import hashlib
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pwdlib import PasswordHash
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AccountModerationLog, PasswordResetToken, User
from scripts.migrate_from_commerce import (
    AccountMigrationConflictError,
    migrate_commerce_accounts,
)
from tests.conftest import TEST_DATABASE_URL

SOURCE_DB_NAME = "commerce_k6_migration_source_test"
SOURCE_DB_URL = (
    f"postgresql+psycopg://{settings.db_admin_user}:{settings.db_admin_password}"
    f"@{settings.db_host}:{settings.db_port}/{SOURCE_DB_NAME}"
)

_hasher = PasswordHash.recommended()
ALICE_PASSWORD = "OldAlicePassword123!"
BOB_PASSWORD = "BobAdminPassword123!"
CHARLIE_PASSWORD = "CharlieBlockedPassword123!"

RAW_USED_TOKEN = "raw_used_token_value_abc"
RAW_EXPIRED_TOKEN = "raw_expired_token_value_xyz"
RAW_VALID_TOKEN = "raw_valid_token_value_123"

HASH_USED_TOKEN = hashlib.sha256(RAW_USED_TOKEN.encode("utf-8")).hexdigest()
HASH_EXPIRED_TOKEN = hashlib.sha256(RAW_EXPIRED_TOKEN.encode("utf-8")).hexdigest()
HASH_VALID_TOKEN = hashlib.sha256(RAW_VALID_TOKEN.encode("utf-8")).hexdigest()


@pytest.fixture
def source_commerce_engine() -> Generator[Any]:
    """Създава изолирана временна Commerce БД с тестови акаунти и я премахва след теста."""
    admin_engine = create_engine(
        settings.admin_database_url, isolation_level="AUTOCOMMIT"
    )
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SOURCE_DB_NAME}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{SOURCE_DB_NAME}"'))

    engine = create_engine(SOURCE_DB_URL, pool_pre_ping=True)

    now = datetime.datetime.now(datetime.UTC)
    past = now - datetime.timedelta(hours=2)
    future = now + datetime.timedelta(hours=2)

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE users (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(32) NOT NULL DEFAULT 'customer',
                    status VARCHAR(32) NOT NULL DEFAULT 'active',
                    display_name VARCHAR(80) NULL,
                    token_version INT NOT NULL DEFAULT 0
                );
                CREATE TABLE account_moderation_logs (
                    id SERIAL PRIMARY KEY,
                    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    admin_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    action VARCHAR(32) NOT NULL,
                    reason TEXT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE TABLE password_reset_tokens (
                    id SERIAL PRIMARY KEY,
                    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash VARCHAR(64) NOT NULL UNIQUE,
                    expires_at TIMESTAMPTZ NOT NULL,
                    used_at TIMESTAMPTZ NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
        )

        alice_hash = _hasher.hash(ALICE_PASSWORD)
        bob_hash = _hasher.hash(BOB_PASSWORD)
        charlie_hash = _hasher.hash(CHARLIE_PASSWORD)

        conn.execute(
            text(
                """
                INSERT INTO users (id, email, password_hash, role, status, display_name, token_version)
                VALUES
                    (101, 'alice@example.com', :alice_hash, 'customer', 'active', 'Alice Wonderland', 3),
                    (202, 'admin_bob@example.com', :bob_hash, 'admin', 'active', 'Bob Admin', 0),
                    (303, 'blocked_charlie@example.com', :charlie_hash, 'customer', 'blocked', 'Charlie Blocked', 1);
                """
            ),
            {
                "alice_hash": alice_hash,
                "bob_hash": bob_hash,
                "charlie_hash": charlie_hash,
            },
        )

        conn.execute(
            text(
                """
                INSERT INTO account_moderation_logs (id, user_id, admin_id, action, reason, created_at)
                VALUES
                    (501, 303, 202, 'block', 'Suspicious spam activities detected', :past);
                """
            ),
            {"past": past},
        )

        conn.execute(
            text(
                """
                INSERT INTO password_reset_tokens (id, user_id, token_hash, expires_at, used_at, created_at)
                VALUES
                    (601, 101, :hash_used, :future, :past, :past),
                    (602, 101, :hash_expired, :past, NULL, :past),
                    (603, 101, :hash_valid, :future, NULL, :now);
                """
            ),
            {
                "hash_used": HASH_USED_TOKEN,
                "hash_expired": HASH_EXPIRED_TOKEN,
                "hash_valid": HASH_VALID_TOKEN,
                "past": past,
                "future": future,
                "now": now,
            },
        )

    yield engine

    engine.dispose()
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SOURCE_DB_NAME}" WITH (FORCE)'))
    admin_engine.dispose()


def test_dry_run_does_not_modify_target_database(
    source_commerce_engine: Any,
    db_session: Session,
) -> None:
    """Dry-run симулацията преброява записите без да променя целевата база данни."""
    users_engine = create_engine(TEST_DATABASE_URL)
    try:
        stats = migrate_commerce_accounts(
            source_commerce_engine, users_engine, dry_run=True
        )
        assert stats.dry_run is True
        assert stats.users_inserted == 3
        assert stats.users_skipped == 0
        assert stats.moderation_logs_inserted == 1
        assert stats.reset_tokens_inserted == 3

        # Проверяваме че целевата база users_test остава абсолютно празна
        count_users = db_session.scalar(select(User))
        count_logs = db_session.scalar(select(AccountModerationLog))
        count_tokens = db_session.scalar(select(PasswordResetToken))
        assert count_users is None
        assert count_logs is None
        assert count_tokens is None
    finally:
        users_engine.dispose()


def test_migrated_user_login_and_profile_and_blocked_user_denial(
    source_commerce_engine: Any,
    client: TestClient,
    db_session: Session,
) -> None:
    """1. Пренесеният потребител влиза със стария парол, GET /users/me връща стария ID; блокираният потребител не може да влезе."""
    users_engine = create_engine(TEST_DATABASE_URL)
    try:
        stats = migrate_commerce_accounts(
            source_commerce_engine, users_engine, dry_run=False
        )
        assert stats.dry_run is False
        assert stats.users_inserted == 3

        # Вход на активния пренесен потребител Alice със стария парол
        resp_login = client.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": ALICE_PASSWORD},
        )
        assert resp_login.status_code == 200
        data = resp_login.json()
        assert "access_token" in data
        token = data["access_token"]

        # GET /users/me връща предишния ID (101) и профил
        resp_me = client.get(
            "/users/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp_me.status_code == 200
        me_data = resp_me.json()
        assert me_data["id"] == 101
        assert me_data["email"] == "alice@example.com"
        assert me_data["role"] == "customer"
        assert me_data["display_name"] == "Alice Wonderland"

        # Вход на блокирания потребител Charlie се отхвърля с 403 ACCOUNT_BLOCKED
        resp_blocked = client.post(
            "/auth/login",
            json={
                "email": "blocked_charlie@example.com",
                "password": CHARLIE_PASSWORD,
            },
        )
        assert resp_blocked.status_code == 403
        assert resp_blocked.json()["detail"]["code"] == "ACCOUNT_BLOCKED"
    finally:
        users_engine.dispose()


def test_moderation_logs_and_password_reset_token_invalidation(
    source_commerce_engine: Any,
    client: TestClient,
    db_session: Session,
) -> None:
    """2. Връзките в одитния журнал са запазени; използваният и изтеклият reset-токен остават невалидни."""
    users_engine = create_engine(TEST_DATABASE_URL)
    try:
        migrate_commerce_accounts(source_commerce_engine, users_engine, dry_run=False)

        # Проверка на одитния журнал за модерация
        log_entry = db_session.get(AccountModerationLog, 501)
        assert log_entry is not None
        assert log_entry.user_id == 303
        assert log_entry.admin_id == 202
        assert log_entry.action == "block"
        assert "Suspicious spam" in (log_entry.reason or "")

        # Опит за използване на вече използвания токен -> отхвърля се (400)
        resp_used = client.post(
            "/auth/password-reset/confirm",
            json={
                "token": RAW_USED_TOKEN,
                "new_password": "BrandNewAlicePassword123!",
            },
        )
        assert resp_used.status_code == 400
        assert resp_used.json()["detail"]["code"] == "INVALID_RESET_TOKEN"

        # Опит за използване на изтеклия токен -> отхвърля се (400)
        resp_expired = client.post(
            "/auth/password-reset/confirm",
            json={
                "token": RAW_EXPIRED_TOKEN,
                "new_password": "BrandNewAlicePassword123!",
            },
        )
        assert resp_expired.status_code == 400
        assert resp_expired.json()["detail"]["code"] == "INVALID_RESET_TOKEN"

        # Опит за използване на валидния неизползван токен -> успява (204)
        resp_valid = client.post(
            "/auth/password-reset/confirm",
            json={
                "token": RAW_VALID_TOKEN,
                "new_password": "BrandNewAlicePassword123!",
            },
        )
        assert resp_valid.status_code == 204

        # Старият парол вече не работи, новият работи
        resp_old_login = client.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": ALICE_PASSWORD},
        )
        assert resp_old_login.status_code == 401

        resp_new_login = client.post(
            "/auth/login",
            json={
                "email": "alice@example.com",
                "password": "BrandNewAlicePassword123!",
            },
        )
        assert resp_new_login.status_code == 200

        # Повторното прилагане на същия токен вече се отхвърля
        resp_repeat = client.post(
            "/auth/password-reset/confirm",
            json={
                "token": RAW_VALID_TOKEN,
                "new_password": "AnotherPassword12345!",
            },
        )
        assert resp_repeat.status_code == 400
    finally:
        users_engine.dispose()


def test_repeatable_migration_and_conflict_handling_and_sequence_sync(
    source_commerce_engine: Any,
    client: TestClient,
    db_session: Session,
) -> None:
    """3. Повторен пренос е безопасен, конфликт не оставя частични данни, регистрация на нов потребител след пренос работи без ID колизии."""
    users_engine = create_engine(TEST_DATABASE_URL)
    try:
        # Първи пренос: записва 3 потребителя, 1 log, 3 токена
        stats_first = migrate_commerce_accounts(
            source_commerce_engine, users_engine, dry_run=False
        )
        assert stats_first.users_inserted == 3
        assert stats_first.users_skipped == 0

        # Повторен пренос на същите данни: всички се пропускат (идемпотентност)
        stats_repeat = migrate_commerce_accounts(
            source_commerce_engine, users_engine, dry_run=False
        )
        assert stats_repeat.users_inserted == 0
        assert stats_repeat.users_skipped == 3
        assert stats_repeat.moderation_logs_inserted == 0
        assert stats_repeat.moderation_logs_skipped == 1
        assert stats_repeat.reset_tokens_inserted == 0
        assert stats_repeat.reset_tokens_skipped == 3

        # Симулация на конфликт: в изходната база променяме данните на Alice (различни роли/права)
        with source_commerce_engine.begin() as conn:
            conn.execute(text("UPDATE users SET role = 'admin' WHERE id = 101"))

        with pytest.raises(AccountMigrationConflictError) as exc_info:
            migrate_commerce_accounts(
                source_commerce_engine, users_engine, dry_run=False
            )
        assert "conflict" in str(exc_info.value).lower()

        # Проверяваме, че конфликтът не е променил данните тихо в целевата база (Alice си остава customer)
        alice_target = db_session.get(User, 101)
        assert alice_target is not None
        assert alice_target.role == "customer"

        # Симулация на частичен запис: връщаме данните на Alice, добавяме един валиден потребител и един конфликтен
        with source_commerce_engine.begin() as conn:
            conn.execute(text("UPDATE users SET role = 'customer' WHERE id = 101"))
            # Валиден нов потребител
            conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role) "
                    "VALUES (404, 'partial_valid@example.com', 'some_hash', 'customer')"
                )
            )
            # Конфликтен потребител: ново ID 888 със съществуващ email на Alice
            # За да позволим дублиран email в изходната база за симулация на конфликт, временно махаме уникалния индекс в изходната
            conn.execute(text("ALTER TABLE users DROP CONSTRAINT users_email_key"))
            conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role) "
                    "VALUES (888, 'alice@example.com', 'different_hash', 'customer')"
                )
            )

        with pytest.raises(AccountMigrationConflictError) as exc_partial:
            migrate_commerce_accounts(
                source_commerce_engine, users_engine, dry_run=False
            )
        assert "conflict" in str(exc_partial.value).lower()

        # Проверяваме, че валидният нов потребител (id=404) НЕ е записан в базата (rollback без частични данни)
        assert db_session.get(User, 404) is None
        assert db_session.get(User, 888) is None

        # Проверка на sequence синхронизацията: регистрация на нов потребител след преноса
        resp_reg = client.post(
            "/users",
            json={
                "email": "brand_new_customer@example.com",
                "password": "BrandNewPassword12345!",
            },
        )
        assert resp_reg.status_code == 201
        new_user = resp_reg.json()
        # Новият ID трябва да е по-голям от максималния пренесен ID (303)
        assert new_user["id"] > 303
        assert new_user["email"] == "brand_new_customer@example.com"
    finally:
        users_engine.dispose()
