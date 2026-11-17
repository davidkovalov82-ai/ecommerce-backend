"""JWT декодиране и верификация за Cart."""

from typing import NamedTuple

import jwt

from app.config import settings


class InvalidToken(Exception):
    """Сигнализира, че JWT токенът е невалиден или изтекъл."""


class TokenData(NamedTuple):
    """Декодирани данни от JWT токен."""

    user_id: int
    role: str
    email: str
    token_version: int


def decode_access_token(
    token: str,
    public_key: str | None = None,
    algorithm: str = "RS256",
    issuer: str = "ecommerce-users",
    audience: str = "ecommerce",
) -> TokenData:
    """Декодира и верифицира RS256 JWT токен от Users."""
    pem = public_key or settings.get_public_key_pem()
    if not pem:
        raise InvalidToken("JWT public key is not configured")

    try:
        payload = jwt.decode(
            token,
            pem,
            algorithms=[algorithm],
            audience=audience,
            issuer=issuer,
            options={
                "require": ["sub", "iat", "exp", "iss", "aud"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_aud": True,
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

    role = payload.get("role", "customer")
    email = payload.get("email", "")
    token_version = payload.get("token_version", 0)

    return TokenData(
        user_id=user_id,
        role=role,
        email=email,
        token_version=token_version,
    )
