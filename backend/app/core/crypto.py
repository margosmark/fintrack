"""At-rest encryption for user secrets (llm_api_key, TOTP seeds) — security review #8.

The Fernet key is `APP_ENCRYPTION_KEY` when configured, and only falls back to
the legacy sha256(`jwt_secret`) derivation when it is not — so an existing
install decrypts its stored ciphertext with no migration, while a deployment
that SETS `APP_ENCRYPTION_KEY` decouples at-rest encryption from token signing
(t_d3176af6): rotating `JWT_SECRET` no longer orphans stored TOTP seeds / LLM
keys.

Rotation never strands ciphertext: `decrypt_secret` tries the primary key, then
every key in `APP_ENCRYPTION_KEY_FALLBACKS`, then the legacy jwt_secret-derived
key. `encrypt_secret` always writes with the primary key, so a row is re-keyed
to the primary the next time it is written. Back `APP_ENCRYPTION_KEY` up WITH
the database (same note as the OFX hash key) — losing it makes stored secrets
unreadable.

`decrypt_secret` returns plaintext input unchanged so rows written before this
migration keep working transparently.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings


def _legacy_key() -> bytes:
    """The pre-t_d3176af6 key: sha256(jwt_secret), urlsafe-b64."""
    digest = hashlib.sha256(get_settings().jwt_secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _parse_fernet_key(raw: str, label: str) -> Fernet:
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"{label} is not a valid Fernet key (urlsafe-b64 of 32 raw bytes); "
            'generate one with `python -c "from cryptography.fernet import '
            'Fernet; print(Fernet.generate_key().decode())"`'
        ) from exc


def _fernet() -> MultiFernet:
    """Primary key first (used for new writes), then every decrypt fallback.

    The legacy jwt_secret-derived key is ALWAYS appended so ciphertext written
    before `APP_ENCRYPTION_KEY` existed — or under an older rotated key the
    operator forgot to list — stays decryptable.
    """
    settings = get_settings()
    keys: list[Fernet] = []
    if settings.app_encryption_key:
        keys.append(_parse_fernet_key(settings.app_encryption_key, "APP_ENCRYPTION_KEY"))
    for raw in (f.strip() for f in settings.app_encryption_key_fallbacks.split(",")):
        if raw:
            keys.append(_parse_fernet_key(raw, "APP_ENCRYPTION_KEY_FALLBACKS entry"))
    keys.append(Fernet(_legacy_key()))
    return MultiFernet(keys)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        # Not a Fernet token ⇒ legacy plaintext written before encryption.
        return value
