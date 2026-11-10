"""Конфигурация на Notifications сервиса чрез Pydantic Settings."""

from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Изолирана PostgreSQL база и роля на notifications
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "notifications"
    db_password: str = "notifications"
    db_name: str = "notifications"

    # Админ данни за автоматично създаване на изолирана тестова БД в pytest
    db_admin_user: str = "commerce"
    db_admin_password: str = "commerce"
    db_admin_name: str = "commerce"

    # RabbitMQ брокер
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "guest"
    rabbitmq_password: str = "guest"
    rabbitmq_vhost: str = "/"
    rabbitmq_exchange: str = "commerce.events"
    rabbitmq_events_queue: str = "notifications.events"
    rabbitmq_dlx_exchange: str = "notifications.dlx"
    rabbitmq_dlq_queue: str = "notifications.events.dlq"
    rabbitmq_dlq_routing_key: str = "notifications.events.dead"
    rabbitmq_prefetch_count: int = 10

    # SMTP / Mailpit
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_from: str = "noreply@example.com"
    smtp_timeout_seconds: float = 5.0
    mailpit_web_url: str = "http://localhost:8025"

    # SMS провайдър (Twilio REST адаптер; по подразбиране безопасен тестов режим)
    sms_mode: Literal["test", "live"] = "test"
    sms_api_base_url: str | None = None
    sms_account_sid: str = "AC00000000000000000000000000000000"
    sms_auth_token: str = "test-twilio-auth-token"
    sms_from_number: str = "+359888000000"
    sms_timeout_seconds: float = 5.0

    # Политики за повторни опити и лезинг на задачите по канали
    email_max_attempts: int = 5
    email_retry_delay_seconds: int = 5
    sms_max_attempts: int = 3
    sms_retry_delay_seconds: int = 2
    delivery_lease_seconds: int = 60
    worker_poll_interval_seconds: float = 0.5

    @model_validator(mode="after")
    def validate_sms_configuration(self) -> "Settings":
        """Гарантира сигурна конфигурация на SMS провайдъра в live и test режим."""
        if self.sms_mode == "live":
            if not self.sms_api_base_url or not self.sms_api_base_url.startswith(
                "https://"
            ):
                raise ValueError(
                    "In live mode, SMS_API_BASE_URL must start with 'https://'"
                )
            if not self.sms_account_sid or self.sms_account_sid.startswith("AC0000"):
                raise ValueError("In live mode, real SMS_ACCOUNT_SID is required")
            if (
                not self.sms_auth_token
                or self.sms_auth_token == "test-twilio-auth-token"
            ):
                raise ValueError("In live mode, real SMS_AUTH_TOKEN is required")
        return self

    @property
    def database_url(self) -> str:
        """Синхронен PostgreSQL URL за собствената база данни на notifications."""
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def admin_database_url(self) -> str:
        """Административен URL само за инициализация на изолираната тестова БД."""
        return (
            f"postgresql+psycopg://{self.db_admin_user}:{self.db_admin_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_admin_name}"
        )


settings = Settings()
