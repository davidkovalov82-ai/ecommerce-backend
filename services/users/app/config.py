"""Конфигурация на услугата Users чрез Pydantic Settings."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки на услугата Users."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Изолирана PostgreSQL база и роля на Users
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "users"
    db_password: str = "users"
    db_name: str = "users"

    # Администраторски данни за създаване на изолирани тестови бази в pytest
    db_admin_user: str = "commerce"
    db_admin_password: str = "commerce"
    db_admin_name: str = "commerce"

    # Порт на услугата
    app_port: int = 8002

    # JWT настройки
    jwt_algorithm: str = "RS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_issuer: str = "ecommerce-users"
    jwt_audience: str = "ecommerce"

    # Асиметрични RSA ключове: директно като PEM текст или път към файл
    jwt_private_key: str = ""
    jwt_public_key: str = ""
    jwt_private_key_path: str = ""
    jwt_public_key_path: str = ""

    @property
    def database_url(self) -> str:
        """Синхронен PostgreSQL URL с psycopg драйвер."""
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def admin_database_url(self) -> str:
        """Администраторски URL за подготовка на тестовата база."""
        return (
            f"postgresql+psycopg://{self.db_admin_user}:{self.db_admin_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_admin_name}"
        )

    def get_private_key(self) -> str:
        """Извлича частния ключ за подписване на JWT токени."""
        if self.jwt_private_key.strip():
            return self.jwt_private_key.replace("\\n", "\n").strip()
        if self.jwt_private_key_path:
            p = Path(self.jwt_private_key_path)
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        raise ValueError(
            "JWT private key is not configured. Set JWT_PRIVATE_KEY or JWT_PRIVATE_KEY_PATH."
        )

    def get_public_key(self) -> str:
        """Извлича публичния ключ за верификация на JWT токени."""
        if self.jwt_public_key.strip():
            return self.jwt_public_key.replace("\\n", "\n").strip()
        if self.jwt_public_key_path:
            p = Path(self.jwt_public_key_path)
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        raise ValueError(
            "JWT public key is not configured. Set JWT_PUBLIC_KEY or JWT_PUBLIC_KEY_PATH."
        )


settings = Settings()
