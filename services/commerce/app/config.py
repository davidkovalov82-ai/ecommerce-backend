from typing import Literal

from pydantic import field_validator, model_validator
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

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15

    stripe_mode: Literal["test", "live"] = "test"
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_success_url: str = "http://localhost:8000/orders/{order_id}?payment=success"
    stripe_cancel_url: str = "http://localhost:8000/orders/{order_id}?payment=cancelled"
    order_reserve_timeout_minutes: int = 30

    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_from: str = "noreply@example.com"
    mailpit_web_url: str = "http://localhost:8025"

    password_reset_base_url: str = "http://localhost:8000/auth/password-reset/confirm"
    password_reset_expire_minutes: int = 15

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        if not v or len(v.encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes long")
        return v

    @model_validator(mode="after")
    def validate_stripe_configuration(self) -> "Settings":
        """Валидация на съвместимостта на Stripe настройките спрямо избрания режим."""
        if self.stripe_mode == "live":
            if self.stripe_secret_key is not None:
                if not self.stripe_secret_key.startswith("sk_live_"):
                    raise ValueError(
                        "In live mode, STRIPE_SECRET_KEY must start with 'sk_live_'"
                    )
                if not self.stripe_webhook_secret:
                    raise ValueError(
                        "In live mode, STRIPE_WEBHOOK_SECRET must be set and non-empty"
                    )
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
        """Синхронен PostgreSQL URL за SQLAlchemy с psycopg v3 драйвер."""
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
