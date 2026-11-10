"""Генерира RSA 2048 двойка ключове (PEM формат) за асиметричен подпис на JWT токени."""

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate_rsa_key_pair(
    private_key_path: Path = Path("jwt_private.pem"),
    public_key_path: Path = Path("jwt_public.pem"),
) -> tuple[str, str]:
    """Генерира RSA 2048 private и public ключове и ги записва в PEM формат."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )

    private_key_path.write_text(private_pem, encoding="utf-8")
    public_key_path.write_text(public_pem, encoding="utf-8")

    return private_pem, public_pem


if __name__ == "__main__":
    priv_path = Path("jwt_private.pem")
    pub_path = Path("jwt_public.pem")
    generate_rsa_key_pair(priv_path, pub_path)
    print(
        f"Generated RSA keys:\n  Private: {priv_path.resolve()}\n  Public:  {pub_path.resolve()}"
    )
