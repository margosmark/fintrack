"""Provision the JWT signing keypairs + a secrets-encryption key (t_d3176af6).

Writes two RSA-2048 keypairs (PEM) — one per signing family — plus a Fernet
`APP_ENCRYPTION_KEY`, and prints the `.env` block that points the API at them.
Run it on the deploy host (or anywhere), keep the output PRIVATE (the `.env`
and `*.pem` files are gitignored; never commit a private key).

    cd backend && uv run python scripts/generate_jwt_keys.py [--out ./secrets]

The API reads the key paths from `.env`:
    JWT_ACCESS_PRIVATE_KEY_PATH / JWT_ACCESS_PUBLIC_KEY_PATH / JWT_ACCESS_KID
    JWT_SESSION_PRIVATE_KEY_PATH / JWT_SESSION_PUBLIC_KEY_PATH / JWT_SESSION_KID
    APP_ENCRYPTION_KEY

Leave those paths EMPTY and the API falls back to an ephemeral in-process
keypair (fine for dev/tests, but tokens then do not survive a restart or a
second worker). `APP_ENCRYPTION_KEY` set means at-rest secret encryption is
decoupled from `JWT_SECRET`; back it up WITH the database.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_KEY_SIZE = 2048


def _write_keypair(out: Path, stem: str) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    priv_path = out / f"{stem}_private.pem"
    pub_path = out / f"{stem}_public.pem"
    priv_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    priv_path.chmod(0o600)
    return priv_path, pub_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="secrets",
        help="directory for the PEM key files (default: ./secrets)",
    )
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    access_priv, access_pub = _write_keypair(out, "jwt_access")
    session_priv, session_pub = _write_keypair(out, "jwt_session")
    app_encryption_key = Fernet.generate_key().decode("ascii")

    print(f"Wrote signing keys to {out}/ (keep private; gitignored):")
    print(f"  access : {access_priv.name} + {access_pub.name}")
    print(f"  session: {session_priv.name} + {session_pub.name}")
    print()
    print("Add these lines to the deployed .env (values are gitignored):")
    print(f"JWT_ACCESS_PRIVATE_KEY_PATH={access_priv.resolve()}")
    print(f"JWT_ACCESS_PUBLIC_KEY_PATH={access_pub.resolve()}")
    print("JWT_ACCESS_KID=access-1")
    print(f"JWT_SESSION_PRIVATE_KEY_PATH={session_priv.resolve()}")
    print(f"JWT_SESSION_PUBLIC_KEY_PATH={session_pub.resolve()}")
    print("JWT_SESSION_KID=session-1")
    print(f"APP_ENCRYPTION_KEY={app_encryption_key}")
    print()
    print("Back APP_ENCRYPTION_KEY up WITH the database — losing it makes stored")
    print("TOTP seeds / LLM keys unreadable. Rotating it? Put the OLD key in")
    print("APP_ENCRYPTION_KEY_FALLBACKS so existing ciphertext stays decryptable.")


if __name__ == "__main__":
    main()
