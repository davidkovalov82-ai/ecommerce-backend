"""Тестове за валидация, подпис и изтичане на JWT токени в Users."""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.tokens import InvalidToken, create_access_token, decode_access_token


def test_create_and_decode_valid_token(test_rsa_keys: tuple[str, str]) -> None:
    """Токен, подписан с валиден private key, се декодира успешно и съдържа всички задължителни claims."""
    private_pem, public_pem = test_rsa_keys

    token = create_access_token(
        user_id=42,
        role="customer",
        email="test42@example.com",
        token_version=1,
        private_key=private_pem,
        expire_minutes=15,
        issuer="ecommerce-users",
        audience="ecommerce",
    )

    data = decode_access_token(
        token=token,
        public_key=public_pem,
        issuer="ecommerce-users",
        audience="ecommerce",
    )

    assert data.user_id == 42
    assert data.role == "customer"
    assert data.email == "test42@example.com"
    assert data.token_version == 1


def test_tampered_token_payload_rejected(test_rsa_keys: tuple[str, str]) -> None:
    """Модифициран токен (променен payload) се отхвърля с InvalidToken."""
    private_pem, public_pem = test_rsa_keys

    token = create_access_token(
        user_id=10,
        role="customer",
        email="user10@example.com",
        token_version=0,
        private_key=private_pem,
    )

    # Променяме символ в средата на токена
    parts = token.split(".")
    tampered_token = f"{parts[0]}.eyJhZG1pbiI6dHJ1ZX0.{parts[2]}"

    with pytest.raises(InvalidToken):
        decode_access_token(tampered_token, public_pem)


def test_tampered_signature_rejected(test_rsa_keys: tuple[str, str]) -> None:
    """Токен с повредена сигнатура се отхвърля с InvalidToken."""
    private_pem, public_pem = test_rsa_keys

    token = create_access_token(
        user_id=10,
        role="customer",
        email="user10@example.com",
        token_version=0,
        private_key=private_pem,
    )

    parts = token.split(".")
    # Сменяме последните символи на сигнатурата
    corrupted_sig = parts[2][:-4] + "AAAA"
    tampered_token = f"{parts[0]}.{parts[1]}.{corrupted_sig}"

    with pytest.raises(InvalidToken):
        decode_access_token(tampered_token, public_pem)


def test_expired_token_rejected(test_rsa_keys: tuple[str, str]) -> None:
    """Просрочен токен (exp в миналото) се отхвърля с InvalidToken."""
    private_pem, public_pem = test_rsa_keys

    token = create_access_token(
        user_id=10,
        role="customer",
        email="user10@example.com",
        token_version=0,
        private_key=private_pem,
        expire_minutes=-5,  # Изтекъл преди 5 минути
    )

    with pytest.raises(InvalidToken):
        decode_access_token(token, public_pem)


def test_token_signed_with_different_private_key_rejected(
    test_rsa_keys: tuple[str, str],
) -> None:
    """Токен, подписан с друг (чужд) RSA частен ключ, се отхвърля при проверка с публичния ключ."""
    _, public_pem = test_rsa_keys

    # Генерираме чужд ключ
    foreign_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    foreign_private_pem = foreign_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    foreign_token = create_access_token(
        user_id=10,
        role="customer",
        email="user10@example.com",
        token_version=0,
        private_key=foreign_private_pem,
    )

    with pytest.raises(InvalidToken):
        decode_access_token(foreign_token, public_pem)


def test_invalid_issuer_rejected(test_rsa_keys: tuple[str, str]) -> None:
    """Токен с несъответстващ issuer се отхвърля."""
    private_pem, public_pem = test_rsa_keys

    token = create_access_token(
        user_id=10,
        role="customer",
        email="user10@example.com",
        token_version=0,
        private_key=private_pem,
        issuer="malicious-issuer",
    )

    with pytest.raises(InvalidToken):
        decode_access_token(token, public_pem, issuer="ecommerce-users")


def test_invalid_audience_rejected(test_rsa_keys: tuple[str, str]) -> None:
    """Токен с несъответстваща audience се отхвърля."""
    private_pem, public_pem = test_rsa_keys

    token = create_access_token(
        user_id=10,
        role="customer",
        email="user10@example.com",
        token_version=0,
        private_key=private_pem,
        audience="wrong-audience",
    )

    with pytest.raises(InvalidToken):
        decode_access_token(token, public_pem, audience="ecommerce")
