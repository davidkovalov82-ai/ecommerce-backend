"""Конфигурация на независимия Payments сервис чрез Pydantic Settings."""

from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Изолирана PostgreSQL база и роля на payments
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "payments"
    db_password: str = "payments"
    db_name: str = "payments"

    # Админ данни само за автоматично създаване на изолирана тестова БД в pytest
    db_admin_user: str = "commerce"
    db_admin_password: str = "commerce"
    db_admin_name: str = "commerce"

    # Вътрешен споделен токен за защита на /internal/payments/*
    internal_auth_token: str = ""

    # Stripe настройки (изолирани единствено в payments)
    stripe_mode: Literal["test", "live"] = "test"
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_api_base: str | None = None
    stripe_success_url: str = "http://localhost:8000/orders/{order_id}?payment=success"
    stripe_cancel_url: str = "http://localhost:8000/orders/{order_id}?payment=cancelled"
    stripe_timeout_seconds: float = 10.0

    # RabbitMQ брокер за изпращане на платежни резултати към Commerce
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "guest"
    rabbitmq_password: str = "guest"
    rabbitmq_vhost: str = "/"
    rabbitmq_exchange: str = "payments.events"
    rabbitmq_commerce_queue: str = "commerce.payment_events"
    rabbitmq_dlx_exchange: str = "commerce.payments.dlx"
    rabbitmq_dlq_queue: str = "commerce.payment_events.dlq"
    rabbitmq_dlq_routing_key: str = "commerce.payment_events.dead"
    rabbitmq_socket_timeout_seconds: float = 5.0

    @field_validator("internal_auth_token", mode="before")
    @classmethod
    def resolve_internal_token_env_alias(cls, v: object) -> str:
        """Поддържа PAYMENTS_INTERNAL_AUTH_TOKEN или internal_auth_token."""
        import os

        if isinstance(v, str) and v.strip():
            return v.strip()
        env_val = os.environ.get("PAYMENTS_INTERNAL_AUTH_TOKEN", "").strip()
        return env_val

    @model_validator(mode="after")
    def validate_stripe_configuration(self) -> "Settings":
        """Валидира конфигурацията на Stripe спрямо избрания режим (test vs live)."""
        if self.stripe_timeout_seconds <= 0:
            raise ValueError("STRIPE_TIMEOUT_SECONDS must be positive")

        if self.stripe_mode == "live":
            if self.stripe_api_base:
                raise ValueError(
                    "In live mode, STRIPE_API_BASE override is not allowed"
                )
            if self.stripe_secret_key is not None and (
                not self.stripe_secret_key.startswith("sk_live_")
            ):
                raise ValueError(
                    "In live mode, STRIPE_SECRET_KEY must start with 'sk_live_'"
                )
            if not self.stripe_webhook_secret:
                raise ValueError("In live mode, STRIPE_WEBHOOK_SECRET must be set")
            if not self.stripe_success_url.startswith("https://"):
                raise ValueError(
                    "In live mode, STRIPE_SUCCESS_URL must start with 'https://'"
                )
            if not self.stripe_cancel_url.startswith("https://"):
                raise ValueError(
                    "In live mode, STRIPE_CANCEL_URL must start with 'https://'"
                )
        elif self.stripe_mode == "test" and (
            self.stripe_secret_key is not None
            and self.stripe_secret_key.startswith("sk_live_")
        ):
            raise ValueError(
                "Live key (sk_live_...) cannot be used in test mode (STRIPE_MODE=test)"
            )
        return self

    @property
    def database_url(self) -> str:
        """Синхронен PostgreSQL URL за собствената база данни на payments."""
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
