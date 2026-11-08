from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import jwt


class InvalidToken(Exception):
    """Сигнализира, че подаденият JWT токен е невалиден, изтекъл или с некоректни claims."""


class TokenData(NamedTuple):
    """Декодирани данни от JWT токен."""

    user_id: int
    token_version: int


def create_access_token(
    user_id: int,
    secret: str,
    algorithm: str = "HS256",
    expire_minutes: int = 15,
    token_version: int = 0,
) -> str:
    """Генерира подписан JWT токен с фиксиран срок на валидност, версия на токена и задължителни claims."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "token_version": token_version,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_access_token(
    token: str,
    secret: str,
    algorithm: str = "HS256",
) -> TokenData:
    """Декодира и верифицира JWT токен, като изисква валиден sub, iat и exp, и връща TokenData."""
    try:
        # Изрично разрешаваме само посочения алгоритъм (HS256), без избор от заглавката
        payload = jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            options={
                "require": ["sub", "iat", "exp"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
            },
        )
    except jwt.PyJWTError as exc:
        raise InvalidToken(f"Token verification failed: {exc}") from exc

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise InvalidToken("Token claim 'sub' must be a non-empty string")

    try:
        user_id = int(sub)
        if user_id <= 0:
            raise ValueError
    except ValueError as exc:
        raise InvalidToken(
            "Token claim 'sub' must represent a positive integer"
        ) from exc

    raw_token_version = payload.get("token_version")
    if raw_token_version is None:
        token_version = 0
    elif isinstance(raw_token_version, int) and raw_token_version >= 0:
        token_version = raw_token_version
    else:
        raise InvalidToken("Token claim 'token_version' must be a non-negative integer")

    return TokenData(user_id=user_id, token_version=token_version)
