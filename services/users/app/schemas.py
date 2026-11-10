"""Pydantic схеми за API на Users."""

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


class UserRegisterRequest(BaseModel):
    """Схема за входните данни при регистрация на потребител."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=15, max_length=128)

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
        """Нормализира имейл адреса (strip и lowercase), без да променя интервалите в паролата."""
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
        Отхвърля нулеви символи (\0) с 422.
        """
        if v is None:
            return None
        if "\0" in v or "\x00" in v:
            raise ValueError("display_name cannot contain null characters")
        cleaned = v.strip()
        if not (1 <= len(cleaned) <= 80):
            raise ValueError("display_name must be between 1 and 80 characters")
        return cleaned


class ChangePasswordRequest(BaseModel):
    """Схема за заявка за смяна на парола на текущия потребител."""

    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str = Field(min_length=15, max_length=128)

    @model_validator(mode="after")
    def verify_passwords_differ(self) -> "ChangePasswordRequest":
        """Проверява дали новата парола се различава от текущата."""
        if self.current_password == self.new_password:
            raise ValueError("New password must be different from current password")
        return self


class PasswordResetRequest(BaseModel):
    """Схема за заявка за изпращане на имейл за възстановяване на парола."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: object) -> object:
        """Нормализира имейл адреса: премахва празни пространства и прави буквите малки."""
        if isinstance(v, str):
            return v.strip().lower()
        return v


class PasswordResetConfirmRequest(BaseModel):
    """Схема за потвърждаване на възстановяването на парола с еднократен токен."""

    model_config = ConfigDict(extra="forbid")

    token: str
    new_password: str = Field(min_length=15, max_length=128)


class PasswordResetRequestResponse(BaseModel):
    """Схема за отговор при заявка за възстановяване на парола (202 Accepted)."""

    message: str


class ModerationRequest(BaseModel):
    """Схема за входни данни при администраторска модерация (блокиране/затваряне)."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class ModerationActionResponse(BaseModel):
    """Схема за резултат от административно модераторско действие."""

    id: int
    email: str
    status: str
    message: str


class InternalUserResponse(BaseModel):
    """Схема за вътрешна валидация на потребител между микросервизи."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: str
    status: str
    token_version: int
    display_name: str | None = None


class HealthResponse(BaseModel):
    """Схема за отговор от health endpoint."""

    status: str = "ok"
    database: str = "ok"
