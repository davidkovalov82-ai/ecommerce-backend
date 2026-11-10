#!/usr/bin/env bash
set -euo pipefail

# scripts/prepare_e2e_keys.sh
# Създава временна RSA-двойка ключове (2048-bit) за изолирани E2E стендове (Users/K4/K5).
# Users получава и двата ключа, Commerce — само публичния.
# Закритият ключ не се извежда и не се коммитва.

TARGET_DIR="${1:-}"

if [[ -z "${TARGET_DIR}" ]]; then
  echo "Грешка: Не е посочена целева директория за временните RSA ключове." >&2
  echo "Употреба: $0 <целева_директория>" >&2
  exit 1
fi

mkdir -p "${TARGET_DIR}"

PRIVATE_KEY="${TARGET_DIR}/jwt_private.pem"
PUBLIC_KEY="${TARGET_DIR}/jwt_public.pem"

if command -v openssl >/dev/null 2>&1; then
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "${PRIVATE_KEY}" 2>/dev/null
  openssl rsa -pubout -in "${PRIVATE_KEY}" -out "${PUBLIC_KEY}" 2>/dev/null
else
  python3 -c "
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
Path('${PRIVATE_KEY}').write_bytes(priv)
Path('${PUBLIC_KEY}').write_bytes(pub)
"
fi

chmod 600 "${PRIVATE_KEY}"
chmod 644 "${PUBLIC_KEY}"

echo "==> E2E RSA keys prepared in ${TARGET_DIR} (private: 600, public: 644)"

