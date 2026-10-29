from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


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


class UserResponse(BaseModel):
    """Публична схема за отговор с данни на потребител (без парола или хеш)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: str
