"""Идемпотентен скрипт и CLI команда за пренос на акаунти от Commerce към Users при K6.1."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, insert, inspect, select, text
from sqlalchemy.orm import Session

# Добавяме services/users към PYTHONPATH, за да са достъпни модулите на app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.models import AccountModerationLog, PasswordResetToken, User
from app.repository import UserRepository

REQUIRED_SOURCE_TABLES = ("users", "account_moderation_logs", "password_reset_tokens")


class AccountMigrationError(Exception):
    """Базова грешка при пренос на акаунти."""


class AccountMigrationConflictError(AccountMigrationError):
    """Грешка при конфликт между данни в изходната и целевата БД."""


@dataclass(frozen=True)
class MigrationStats:
    """Статистика за изпълнения или симулиран пренос на акаунти."""

    dry_run: bool
    users_inserted: int = 0
    users_skipped: int = 0
    moderation_logs_inserted: int = 0
    moderation_logs_skipped: int = 0
    reset_tokens_inserted: int = 0
    reset_tokens_skipped: int = 0

    @property
    def total_to_insert(self) -> int:
        return (
            self.users_inserted
            + self.moderation_logs_inserted
            + self.reset_tokens_inserted
        )

    @property
    def total_skipped(self) -> int:
        return (
            self.users_skipped + self.moderation_logs_skipped + self.reset_tokens_skipped
        )


def _validate_distinct_databases(commerce_engine: Engine, users_engine: Engine) -> None:
    """Проверява, че изходната и целевата база данни са различни."""
    with commerce_engine.connect() as c_conn, users_engine.connect() as u_conn:
        c_db = c_conn.scalar(text("SELECT current_database()"))
        u_db = u_conn.scalar(text("SELECT current_database()"))
        c_host = c_conn.scalar(text("SELECT inet_server_addr()"))
        u_host = u_conn.scalar(text("SELECT inet_server_addr()"))
        c_port = c_conn.scalar(text("SELECT inet_server_port()"))
        u_port = u_conn.scalar(text("SELECT inet_server_port()"))

        if c_db == u_db and c_host == u_host and c_port == u_port:
            raise AccountMigrationError(
                "Source and target databases must be different databases"
            )


def _validate_distinct_databases(commerce_engine: Engine, users_engine: Engine) -> None:
    """Проверява, че изходната и целевата база данни са различни."""
    with commerce_engine.connect() as c_conn, users_engine.connect() as u_conn:
        c_db = c_conn.scalar(text("SELECT current_database()"))
        u_db = u_conn.scalar(text("SELECT current_database()"))
        c_host = c_conn.scalar(text("SELECT inet_server_addr()"))
        u_host = u_conn.scalar(text("SELECT inet_server_addr()"))
        c_port = c_conn.scalar(text("SELECT inet_server_port()"))
        u_port = u_conn.scalar(text("SELECT inet_server_port()"))

        if c_db == u_db and c_host == u_host and c_port == u_port:
            raise AccountMigrationError("Source and target databases must be different databases")


def _fetch_source_records(commerce_engine: Engine) -> dict[str, list[dict[str, Any]]]:
    """Проверява наличието на задължителните таблици и извлича данни от изходната Commerce БД."""
    with commerce_engine.connect() as conn:
        inspector = inspect(conn)
        existing_tables = set(inspector.get_table_names())

        missing = [t for t in REQUIRED_SOURCE_TABLES if t not in existing_tables]
        if missing:
            raise AccountMigrationError(
                f"Source database is missing required table(s): {', '.join(missing)}"
            )

        users_rows: list[dict[str, Any]] = [
            dict(r)
            for r in conn.execute(
                text(
                    """
                    SELECT id, email, password_hash, role, status, display_name, token_version
                    FROM users
                    ORDER BY id ASC
                    """
                )
            )
            .mappings()
            .all()
        ]

        logs_rows: list[dict[str, Any]] = [
            dict(r)
            for r in conn.execute(
                text(
                    """
                    SELECT id, user_id, admin_id, action, reason, created_at
                    FROM account_moderation_logs
                    ORDER BY id ASC
                    """
                )
            )
            .mappings()
            .all()
        ]

        tokens_rows: list[dict[str, Any]] = [
            dict(r)
            for r in conn.execute(
                text(
                    """
                    SELECT id, user_id, token_hash, expires_at, used_at, created_at
                    FROM password_reset_tokens
                    ORDER BY id ASC
                    """
                )
            )
            .mappings()
            .all()
        ]

    return {
        "users": users_rows,
        "moderation_logs": logs_rows,
        "password_reset_tokens": tokens_rows,
    }


def migrate_commerce_accounts(
    commerce_engine: Engine,
    users_engine: Engine,
    dry_run: bool = True,
) -> MigrationStats:
    """Пренася потребители, модераторски одит логове и токени за ресет от Commerce към Users.

    - Проверява, че източникът и целта са различни бази данни.
    - Проверява за наличието на всички задължителни таблици в източника.
    - При dry_run=True: проверява за конфликти и преброява записи без промени по целевата БД.
    - При dry_run=False: изпълнява всички записи в една транзакция с rollback при грешка/конфликт.
    - При повторно изпълнение на идентични данни: пропуска съществуващите записи (идемпотентност).
    - При несъответствие на ID/email или съдържание: спира с AccountMigrationConflictError без частичен запис.
    - Не намалява sequence броячите на ID.
    - В съобщенията за грешка никога не извежда email адреси, паролни хешове, токени или connection strings.
    """
    _validate_distinct_databases(commerce_engine, users_engine)
    source_data = _fetch_source_records(commerce_engine)
    source_users = source_data["users"]
    source_logs = source_data["moderation_logs"]
    source_tokens = source_data["password_reset_tokens"]

    with Session(users_engine) as session:
        try:
            # 1. Валидация на users
            users_to_insert: list[dict[str, Any]] = []
            users_skipped = 0

            for u in source_users:
                u_id = u["id"]
                u_email = u["email"].strip().lower()

                existing_by_id = session.get(User, u_id)
                existing_by_email = session.scalar(
                    select(User).where(User.email == u_email)
                )

                if existing_by_id is not None and existing_by_email is not None:
                    if existing_by_id.id != existing_by_email.id:
                        raise AccountMigrationConflictError(
                            f"User conflict for id={u_id}: ID and email belong to different users in target database"
                        )
                    # Един и същ запис - проверяваме дали всички полета съвпадат точно
                    target_user = existing_by_id
                    matches = (
                        target_user.email == u_email
                        and target_user.password_hash == u["password_hash"]
                        and target_user.role == u["role"]
                        and target_user.status == u["status"]
                        and target_user.display_name == u["display_name"]
                        and target_user.token_version == u["token_version"]
                    )
                    if matches:
                        users_skipped += 1
                    else:
                        raise AccountMigrationConflictError(
                            f"User conflict for id={u_id}: user attributes in target database diverge from source"
                        )
                elif existing_by_id is not None and existing_by_email is None:
                    raise AccountMigrationConflictError(
                        f"User conflict for id={u_id}: target database already has user with this ID but different attributes"
                    )
                elif existing_by_id is None and existing_by_email is not None:
                    raise AccountMigrationConflictError(
                        f"User conflict for id={u_id}: email is already registered under a different ID in target database"
                    )
                else:
                    users_to_insert.append(
                        {
                            "id": u_id,
                            "email": u_email,
                            "password_hash": u["password_hash"],
                            "role": u["role"],
                            "status": u["status"],
                            "display_name": u["display_name"],
                            "token_version": u["token_version"],
                        }
                    )

            # 2. Валидация на account_moderation_logs
            logs_to_insert: list[dict[str, Any]] = []
            logs_skipped = 0

            for m in source_logs:
                m_id = m["id"]
                existing_log = session.get(AccountModerationLog, m_id)

                if existing_log is not None:
                    matches = (
                        existing_log.user_id == m["user_id"]
                        and existing_log.admin_id == m["admin_id"]
                        and existing_log.action == m["action"]
                        and existing_log.reason == m["reason"]
                        and existing_log.created_at == m["created_at"]
                    )
                    if matches:
                        logs_skipped += 1
                    else:
                        raise AccountMigrationConflictError(
                            f"Account moderation log conflict for id={m_id}: existing record attributes diverge from source"
                        )
                else:
                    logs_to_insert.append(m)

            # 3. Валидация на password_reset_tokens
            tokens_to_insert: list[dict[str, Any]] = []
            tokens_skipped = 0

            for t in source_tokens:
                t_id = t["id"]
                t_hash = t["token_hash"]

                existing_by_id = session.get(PasswordResetToken, t_id)
                existing_by_hash = session.scalar(
                    select(PasswordResetToken).where(
                        PasswordResetToken.token_hash == t_hash
                    )
                )

                if existing_by_id is not None and existing_by_hash is not None:
                    if existing_by_id.id != existing_by_hash.id:
                        raise AccountMigrationConflictError(
                            f"Password reset token conflict for id={t_id}: ID and token hash match different records"
                        )
                    matches = (
                        existing_by_id.user_id == t["user_id"]
                        and existing_by_id.expires_at == t["expires_at"]
                        and existing_by_id.used_at == t["used_at"]
                        and existing_by_id.created_at == t["created_at"]
                    )
                    if matches:
                        tokens_skipped += 1
                    else:
                        raise AccountMigrationConflictError(
                            f"Password reset token conflict for id={t_id}: existing record attributes diverge from source"
                        )
                elif existing_by_id is not None or existing_by_hash is not None:
                    raise AccountMigrationConflictError(
                        f"Password reset token conflict for id={t_id}: ID or token hash already exists with divergent attributes"
                    )
                else:
                    tokens_to_insert.append(t)

            stats = MigrationStats(
                dry_run=dry_run,
                users_inserted=len(users_to_insert),
                users_skipped=users_skipped,
                moderation_logs_inserted=len(logs_to_insert),
                moderation_logs_skipped=logs_skipped,
                reset_tokens_inserted=len(tokens_to_insert),
                reset_tokens_skipped=tokens_skipped,
            )

            if dry_run:
                session.rollback()
                return stats

            # Режим Apply: запис в една транзакция
            if users_to_insert:
                session.execute(insert(User), users_to_insert)
                session.flush()

            if logs_to_insert:
                session.execute(insert(AccountModerationLog), logs_to_insert)
                session.flush()

            if tokens_to_insert:
                session.execute(insert(PasswordResetToken), tokens_to_insert)
                session.flush()

            UserRepository(session).sync_sequences()
            session.commit()

            return stats
        except Exception:
            session.rollback()
            raise


def build_parser() -> argparse.ArgumentParser:
    """Конструира парсера за CLI аргументи на миграцията."""
    parser = argparse.ArgumentParser(
        description="Пренос на акаунти, модерация и токени от Commerce към Users при K6.1."
    )
    parser.add_argument(
        "--commerce-url",
        required=True,
        help="PostgreSQL URL на изходната Commerce БД (само за четене по време на преноса).",
    )
    parser.add_argument(
        "--users-url",
        default=settings.database_url,
        help="PostgreSQL URL на целевата Users БД (по подразбиране от settings.database_url).",
    )
    parser.add_argument(
        "--mode",
        choices=["dry-run", "apply"],
        default=None,
        help="Режим на работа: 'dry-run' (по подразбиране) или 'apply'.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Прилагане на промените в целевата БД (еквивалентно на --mode apply).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Само симулация без промени по базата данни (еквивалентно на --mode dry-run).",
    )
    return parser


def parse_migration_args(
    parser: argparse.ArgumentParser, argv: list[str] | None = None
) -> tuple[argparse.Namespace, bool]:
    """Парсира аргументите и валидира режима ПРЕДИ каквото и да е свързване към БД."""
    args = parser.parse_args(argv)

    has_dry_run = args.dry_run or (args.mode == "dry-run")
    has_apply = args.apply or (args.mode == "apply")

    if has_dry_run and has_apply:
        parser.error(
            "Conflicting mode options: specify either --dry-run or --apply, not both."
        )

    # По подразбиране е dry-run
    is_dry_run = not has_apply
    return args, is_dry_run


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args, is_dry_run = parse_migration_args(parser, argv)

    has_dry_run = args.dry_run or (args.mode == "dry-run")
    has_apply = args.apply or (args.mode == "apply")

    if has_dry_run and has_apply:
        parser.error("Conflicting mode options: specify either --dry-run or --apply, not both.")

    # По подразбиране е dry-run
    is_dry_run = not has_apply
    return args, is_dry_run


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args, is_dry_run = parse_migration_args(parser, argv)

    c_engine = create_engine(args.commerce_url, pool_pre_ping=True)
    u_engine = create_engine(args.users_url, pool_pre_ping=True)

    mode_label = "DRY-RUN (симулация без запис)" if is_dry_run else "APPLY (запис)"
    print(f"Стартиране на пренос на акаунти в режим: {mode_label}")

    try:
        stats = migrate_commerce_accounts(c_engine, u_engine, dry_run=is_dry_run)
        action_verb = "биха били добавени" if is_dry_run else "добавени"
        print(
            f"Преносът завърши успешно ({mode_label}):\n"
            f"  - Потребители: {stats.users_inserted} {action_verb}, {stats.users_skipped} пропуснати (вече съществуващи)\n"
            f"  - Одит логове: {stats.moderation_logs_inserted} {action_verb}, {stats.moderation_logs_skipped} пропуснати\n"
            f"  - Reset токени: {stats.reset_tokens_inserted} {action_verb}, {stats.reset_tokens_skipped} пропуснати"
        )
    except AccountMigrationError as exc:
        print(f"ГРЕШКА ПРИ ПРЕНОСА: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        # Без показване на SQL параметри, пароли или connection strings
        print(
            f"ГРЕШКА ПРИ ПРЕНОСА: неочаквана грешка ({type(exc).__name__})",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        # Без показване на SQL параметри, пароли или connection strings
        print(f"ГРЕШКА ПРИ ПРЕНОСА: неочаквана грешка ({type(exc).__name__})", file=sys.stderr)
        sys.exit(1)
    finally:
        c_engine.dispose()
        u_engine.dispose()


if __name__ == "__main__":
    main()
