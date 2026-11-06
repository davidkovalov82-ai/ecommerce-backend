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

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15

    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_success_url: str = "http://localhost:8000/orders/{order_id}?payment=success"
    stripe_cancel_url: str = "http://localhost:8000/orders/{order_id}?payment=cancelled"

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        if not v or len(v.encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes long")
        return v

    @property
    def database_url(self) -> str:
        """Синхронен PostgreSQL URL за SQLAlchemy с psycopg v3 драйвер."""
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
