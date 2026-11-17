"""Конфигурация на услугата Cart чрез Pydantic Settings."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки на услугата Cart."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Изолирана PostgreSQL база и роля на Cart
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "cart"
    db_password: str = "cart"
    db_name: str = "cart"

    # Администраторски данни за създаване на изолирани тестови бази в pytest
    db_admin_user: str = "commerce"
    db_admin_password: str = "commerce"
    db_admin_name: str = "commerce"

    # Порт на услугата
    app_port: int = 8004

    # Споделен таен токен за междусервизен вътрешен достъп (/internal/*)
    internal_auth_token: str = "ecommerce-cart-internal-secret"

    # Връзка с Catalog за проверка на наличност и извличане на актуални цени
    catalog_base_url: str = "http://localhost:8003"
    catalog_internal_auth_token: str = "ecommerce-catalog-internal-secret"
    catalog_timeout_seconds: float = 5.0

    # JWT настройки за верификация на токени, издадени от Users
    jwt_algorithm: str = "RS256"
    jwt_issuer: str = "ecommerce-users"
    jwt_audience: str = "ecommerce"
    jwt_public_key: str = ""
    jwt_public_key_path: str = ""

    @property
    def database_url(self) -> str:
        """Генерира стандартен SQLAlchemy URL адрес за връзка с PostgreSQL."""
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def admin_database_url(self) -> str:
        """Администраторски URL адрес за подготовка на тестовата база в pytest."""
        return (
            f"postgresql+psycopg://{self.db_admin_user}:{self.db_admin_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_admin_name}"
        )

    def get_public_key_pem(self) -> str:
        """Извлича публичния RSA ключ като PEM низ (от променлива или файл)."""
        if self.jwt_public_key and self.jwt_public_key.strip():
            return self.jwt_public_key.strip()

        candidate_paths: list[Path] = []
        if self.jwt_public_key_path:
            candidate_paths.append(Path(self.jwt_public_key_path))
        candidate_paths.extend(
            [
                Path("jwt_public.pem"),
                Path("/app/jwt_public.pem"),
                Path(__file__).resolve().parent.parent / "jwt_public.pem",
                Path(__file__).resolve().parent.parent.parent
                / "users"
                / "jwt_public.pem",
            ]
        )

        for path in candidate_paths:
            if path.exists() and path.is_file():
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    return content

        return ""


settings = Settings()
