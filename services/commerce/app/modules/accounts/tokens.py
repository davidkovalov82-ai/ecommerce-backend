"""Генериране и верификация на JWT токени за Commerce.

След отделянето на микросервиса Users, Commerce приема само асиметрично
подписани RS256 токени, издадени от Users с issuer 'ecommerce-users'
и audience 'ecommerce'.
"""

import os
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import jwt


class InvalidToken(Exception):
    """Сигнализира, че подаденият JWT токен е невалиден, изтекъл или с некоректни claims."""


class TokenData(NamedTuple):
    """Декодирани данни от JWT токен."""

    user_id: int
    role: str
    email: str
    token_version: int


def create_access_token(
    user_id: int,
    secret: str | None = None,
    private_key: str | None = None,
    algorithm: str = "RS256",
    expire_minutes: int = 15,
    token_version: int = 0,
    role: str = "customer",
    email: str = "",
    issuer: str = "ecommerce-users",
    audience: str = "ecommerce",
) -> str:
    """Генерира подписан JWT access-токен (за тестове и интеграция)."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "email": email,
        "token_version": token_version,
        "iss": issuer,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expire_minutes)).timestamp()),
    }
    key = private_key
    if not key and secret and "BEGIN" in str(secret):
        key = secret
    if not key and secret and secret != os.environ.get("JWT_SECRET", "legacy_jwt_secret_at_least_32_bytes_long_fallback") and "BEGIN" not in str(secret):
        return jwt.encode(payload, secret, algorithm="HS256")
    if not key:
        key = os.environ.get("JWT_PRIVATE_KEY", "")
    if not key:
        raise ValueError("RSA private key is required to sign RS256 token")

    return jwt.encode(payload, key, algorithm="RS256")


def decode_access_token(
    token: str,
    public_key: str,
    algorithm: str = "RS256",
    issuer: str = "ecommerce-users",
    audience: str = "ecommerce",
) -> TokenData:
    """Декодира и верифицира RS256 JWT токен от Users по публичен RSA ключ.

    Стари HS256 токени или токени с невалидна подпись, срок, issuer или audience
    се отхвърлят с InvalidToken.
    """
    if not public_key:
        raise InvalidToken("JWT public key is not configured")

    try:
        # Изрично разрешаваме само посочения алгоритъм (RS256), без избор от заглавката
        payload = jwt.decode(
            token,
            public_key,
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

    role = payload.get("role")
    if not isinstance(role, str) or not role:
        role = "customer"

    email = payload.get("email")
    if not isinstance(email, str) or not email:
        email = ""

    raw_token_version = payload.get("token_version")
    if raw_token_version is None:
        token_version = 0
    elif isinstance(raw_token_version, int) and raw_token_version >= 0:
        token_version = raw_token_version
    else:
        raise InvalidToken("Token claim 'token_version' must be a non-negative integer")

    return TokenData(
        user_id=user_id,
        role=role,
        email=email,
        token_version=token_version,
    )
