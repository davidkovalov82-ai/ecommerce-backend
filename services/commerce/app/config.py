from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "commerce"
    db_password: str = "commerce"
    db_name: str = "commerce"
    db_admin_user: str | None = None
    db_admin_password: str | None = None
    db_admin_name: str | None = None

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15

    # Независим Payments сервис (Stripe ключовете живеят само в services/payments)
    payments_base_url: str = "http://localhost:8001"
    payments_internal_auth_token: str = ""
    payments_timeout_seconds: float = 10.0
    order_reserve_timeout_minutes: int = 30

    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_from: str = "noreply@example.com"
    mailpit_web_url: str = "http://localhost:8025"

    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "guest"
    rabbitmq_password: str = "guest"
    rabbitmq_vhost: str = "/"
    rabbitmq_exchange: str = "commerce.events"
    rabbitmq_notifications_queue: str = "notifications.events"
    rabbitmq_dlx_exchange: str = "notifications.dlx"
    rabbitmq_dlq_queue: str = "notifications.events.dlq"
    rabbitmq_dlq_routing_key: str = "notifications.events.dead"
    rabbitmq_payments_exchange: str = "payments.events"
    rabbitmq_payments_queue: str = "commerce.payment_events"
    rabbitmq_payments_dlx_exchange: str = "commerce.payments.dlx"
    rabbitmq_payments_dlq_queue: str = "commerce.payment_events.dlq"
    rabbitmq_payments_dlq_routing_key: str = "commerce.payment_events.dead"
    rabbitmq_prefetch_count: int = 10
    rabbitmq_socket_timeout_seconds: float = 5.0

    password_reset_base_url: str = "http://localhost:8000/auth/password-reset/confirm"
    password_reset_expire_minutes: int = 15

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        if not v or len(v.encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes long")
        return v

    @field_validator("payments_internal_auth_token", mode="before")
    @classmethod
    def resolve_payments_token_alias(cls, v: object) -> str:
        import os

        if isinstance(v, str) and v.strip():
            return v.strip()
        return os.environ.get("PAYMENTS_INTERNAL_AUTH_TOKEN", "").strip()

    @field_validator("payments_timeout_seconds")
    @classmethod
    def validate_payments_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("PAYMENTS_TIMEOUT_SECONDS must be positive")
        return v

    @property
    def database_url(self) -> str:
        """Синхронен PostgreSQL URL за SQLAlchemy с psycopg v3 драйвер."""
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def admin_database_url(self) -> str:
        """Административен URL само за управление на изолирани тестови БД/роли."""
        admin_user = self.db_admin_user or self.db_user
        admin_password = (
            self.db_admin_password
            if self.db_admin_password is not None
            else self.db_password
        )
        admin_name = self.db_admin_name or self.db_name
        return (
            f"postgresql+psycopg://{admin_user}:{admin_password}"
            f"@{self.db_host}:{self.db_port}/{admin_name}"
        )


settings = Settings()
