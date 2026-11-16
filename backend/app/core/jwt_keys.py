"""RS256 signing-key management for server-signed JWTs (t_d3176af6, docs/01).

Two independent key FAMILIES, each provisioned from a PEM key file pair:

- ``access``  — signs ``type=access`` bearer tokens.
- ``session`` — signs ``type=challenge`` (2FA step-up) and ``type=oidc_pending``
  (the OIDC hand-off blob); the two families that ESCALATE into a new session.

Splitting them means one compromised key can no longer mint every token kind:
an access-key leak cannot forge a long-lived session, and a session-key leak
cannot mint bearer tokens.

When the ``JWT_{FAMILY}_{PRIVATE,PUBLIC}_KEY_PATH`` settings are empty (the
dev/test default) an EPHEMERAL in-process RSA-2048 keypair is generated once
per family and cached — zero-config, still RS256, but tokens then do not
survive a restart or a second worker, so a deployment MUST set the four paths
(``scripts/generate_jwt_keys.py`` writes them).

Every token carries a ``kid`` header pinned to the family's configured KID, and
verification re-checks that ``kid`` before decoding with a HARDCODED
``RS256`` — never ``alg: none``, never a caller-supplied algorithm (the
strictness of ``services/oidc.py``).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_RSA_KEY_SIZE = 2048
# The only algorithm this app ever signs or verifies a server JWT with. It is
# hardcoded at the decode call site too, so a token's own ``alg`` header can
# never select the verification algorithm (the alg-confusion class of bug).
ALGORITHM = "RS256"


@dataclass(frozen=True)
class FamilyKeys:
    """The loaded signing (private) + verification (public) halves + KID."""

    private_pem: str
    public_pem: str
    kid: str


# Cache key -> FamilyKeys. Keyed on (family, priv_path, pub_path, kid) so a
# settings change (new file paths, rotated KID) naturally misses and reloads,
# while the stable dev/test ephemeral keys survive `get_settings.cache_clear()`.
_cache: dict[tuple[str, str, str, str], FamilyKeys] = {}
_cache_lock = threading.Lock()


def _generate_ephemeral() -> tuple[str, str]:
    """A fresh in-memory RSA keypair as (private_pem, public_pem)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=_RSA_KEY_SIZE)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


def _load_family(family: str, priv_path: str, pub_path: str, kid: str) -> FamilyKeys:
    if priv_path:
        try:
            private_pem = Path(priv_path).read_text(encoding="ascii")
        except OSError as exc:
            raise RuntimeError(
                f"JWT {family.upper()} private key file is not readable "
                f"({priv_path}): {exc}. Set both key paths or leave both empty "
                "for an ephemeral dev keypair."
            ) from exc
        try:
            public_pem = Path(pub_path).read_text(encoding="ascii")
        except OSError as exc:
            raise RuntimeError(
                f"JWT {family.upper()} public key file is not readable "
                f"({pub_path}): {exc}. Set both key paths or leave both empty "
                "for an ephemeral dev keypair."
            ) from exc
        return FamilyKeys(private_pem=private_pem, public_pem=public_pem, kid=kid)
    # Dev/test path: an ephemeral in-process keypair. Loud once per family so a
    # production deployment that forgot to provision key files cannot mistake
    # restart-ephemeral tokens for stable ones.
    private_pem, public_pem = _generate_ephemeral()
    logger.warning(
        "JWT %s signing key is EPHEMERAL (no key file configured); tokens will "
        "not survive a restart or a second worker. Set %s_PRIVATE_KEY_PATH / "
        "%s_PUBLIC_KEY_PATH in production.",
        family.upper(),
        f"JWT_{family.upper()}",
        f"JWT_{family.upper()}",
    )
    return FamilyKeys(private_pem=private_pem, public_pem=public_pem, kid=kid)


def get_family_keys(family: str) -> FamilyKeys:
    """Load (and cache) the signing + verification keys for a family."""
    if family not in ("access", "session"):
        raise ValueError(f"unknown JWT key family: {family!r}")
    settings = get_settings()
    priv_path = getattr(settings, f"jwt_{family}_private_key_path")
    pub_path = getattr(settings, f"jwt_{family}_public_key_path")
    kid = getattr(settings, f"jwt_{family}_kid")
    cache_key = (family, priv_path, pub_path, kid)
    with _cache_lock:
        keys = _cache.get(cache_key)
        if keys is None:
            keys = _load_family(family, priv_path, pub_path, kid)
            _cache[cache_key] = keys
        return keys


def clear_key_cache() -> None:
    """Drop cached keys (test hook — forces a reload on the next call)."""
    with _cache_lock:
        _cache.clear()


def preload_signing_keys() -> dict[str, str]:
    """Eagerly load BOTH families at startup (t_adfb7881, docs/38 §Key provisioning).

    Returns ``{family: source}`` where source is ``file-backed (<path>)`` or
    ``EPHEMERAL (dev/test; tokens die on restart)`` — the boot log line that
    tells an operator which mode the api came up in.

    Raises ``RuntimeError`` (the ``_load_family`` hard-fail) when a configured
    path is unreadable: a bad compose secrets mount must fail the BOOT with a
    named error, not surface as 500s on the first login after a restart — and
    never silently mint a fresh ephemeral keypair, which is the very failure
    this provisioning exists to eliminate.
    """
    settings = get_settings()
    sources: dict[str, str] = {}
    for family in ("access", "session"):
        get_family_keys(family)
        path = getattr(settings, f"jwt_{family}_private_key_path")
        sources[family] = (
            f"file-backed ({path})" if path else "EPHEMERAL (dev/test; tokens die on restart)"
        )
    return sources


def encode_jwt(family: str, payload: dict) -> str:
    """Sign `payload` with the family's RS256 key, stamping the `kid` header."""
    keys = get_family_keys(family)
    return jwt.encode(payload, keys.private_pem, algorithm=ALGORITHM, headers={"kid": keys.kid})


def decode_jwt(family: str, token: str) -> dict:
    """Verify `token` against the family's RS256 public key.

    Raises ``jwt.PyJWTError`` for any malformed, tampered, expired, wrong-`kid`
    or wrong-algorithm token. The algorithm is HARDCODED to RS256 (never read
    from the token), so ``alg: none`` and algorithm-confusion are impossible.
    """
    keys = get_family_keys(family)
    # Pin the `kid` BEFORE trusting any claim: a token minted by another
    # family's key (or a rotated-out key) is rejected even if it would
    # otherwise verify. `get_unverified_header` is safe here precisely because
    # nothing downstream trusts it — it only selects the expected KID.
    header = jwt.get_unverified_header(token)
    if header.get("alg") != ALGORITHM:
        raise jwt.InvalidTokenError(f"unsupported token alg {header.get('alg')!r}")
    if header.get("kid") != keys.kid:
        raise jwt.InvalidTokenError("token kid does not match this key")
    return jwt.decode(
        token,
        keys.public_pem,
        algorithms=[ALGORITHM],
        options={"require": ["exp", "iat", "sub"]},
    )
