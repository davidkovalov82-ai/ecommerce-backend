from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class UserRegisterRequest(BaseModel):
    """Схема за входните данни при регистрация на потребител."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=15, max_length=128)

    # как работи field_validator и classmethod защо ги исползваме тук
    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: object) -> object:
        """Валидира и нормализира имейл адреса: премахва външни интервали и прави буквите малки."""
        if isinstance(v, str):
            return v.strip().lower()
        return v


class UserLoginRequest(BaseModel):
    """Схема за входните данни при вход (логин) на потребител."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: object) -> object:
        """Нормализира имейл адреса (strip и lowercase), без да променя паролата."""
        if isinstance(v, str):
            return v.strip().lower()
        return v


class TokenResponse(BaseModel):
    """Схема за отговор при успешен вход с издаден Bearer JWT токен."""

    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    """Публична схема за отговор с данни на потребител (без парола или хеш)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: str
    display_name: str | None = None


class UserUpdateRequest(BaseModel):
    """Схема за редактиране на потребителски профил (display_name)."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, v: str | None) -> str | None:
        """Нормализира и валидира display_name.

        Явен null връща None (изчистване на името).
        За низове премахва външните интервали и изисква дължина между 1 и 80 знака.
        """
        if v is None:
            return None
        cleaned = v.strip()
        if not (1 <= len(cleaned) <= 80):
            raise ValueError("display_name must be between 1 and 80 characters")
        return cleaned
