"""RFC 6238 TOTP + recovery codes (M74 — docs/38).

Pure functions over stdlib `hmac`/`hashlib` — no dependency, no I/O, no app
state, so the algorithm can be pinned by the RFC's own test vectors in
`tests/test_auth_totp.py` instead of by our own implementation's output.

Parameters are the ones every authenticator app ships: SHA-1, 6 digits, 30 s
step, verified over a ±1-step window.
"""

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import UTC, datetime

DIGITS = 6
PERIOD_SECONDS = 30
# Verification window: t-1, t, t+1 (clock skew of authenticator apps).
WINDOW = 1

# No 0/O/1/I/L/U — codes are read off a screen and typed on a phone keyboard.
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
RECOVERY_CODE_COUNT = 10
_RECOVERY_GROUP = 5


def new_secret() -> str:
    """A fresh 160-bit TOTP secret as unpadded base32 (RFC 4648)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def otpauth_url(secret: str, email: str, issuer: str) -> str:
    """The `otpauth://` URI an authenticator app scans or types in."""
    from urllib.parse import quote

    label = f"{quote(issuer, safe='')}:{quote(email, safe='')}"
    params = (
        f"secret={secret}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD_SECONDS}"
    )
    return f"otpauth://totp/{label}?{params}"


def _hotp(secret_b32: str, counter: int) -> str:
    """HOTP (RFC 4226) over `counter`, then truncated to 6 digits."""
    padding = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32 + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**DIGITS)
    return f"{code:0{DIGITS}d}"


def step_at(at: datetime) -> int:
    return int(at.timestamp()) // PERIOD_SECONDS


def totp_code(secret: str, at: datetime) -> str:
    """The code valid for `at` (defaults to now at the call site)."""
    return _hotp(secret, step_at(at))


def verify_totp(secret: str, code: str, at: datetime, last_used_step: int | None) -> int | None:
    """Accept `code` iff it matches step t-1/t/t+1 AND has not been used yet.

    Returns the accepted step (the caller persists it as `last_used_step`) or
    None. Wrong, expired and replayed codes are deliberately indistinguishable
    — one outcome, one error code, one client failure path.
    """
    if not (code.isdigit() and len(code) == DIGITS):
        return None
    step = step_at(at)
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if candidate < 0:
            continue
        if hmac.compare_digest(_hotp(secret, candidate), code):
            if last_used_step is not None and candidate <= last_used_step:
                return None  # replay of an already-consumed step
            return candidate
    return None


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """`count` single-use codes, `XXXXX-XXXXX` (~49 bits each)."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(2 * _RECOVERY_GROUP))
        codes.append(f"{raw[:_RECOVERY_GROUP]}-{raw[_RECOVERY_GROUP:]}")
    return codes


def normalize_recovery(code: str) -> str:
    """Uppercase and drop separators so `abcde-fghjk` == `ABCDEFGHIJK`-less."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def hash_recovery(code: str) -> str:
    return hashlib.sha256(normalize_recovery(code).encode("utf-8")).hexdigest()


def is_totp_shaped(code: str) -> bool:
    r"""`^\d{6}$` — routes a disable attempt to the TOTP or recovery error."""
    return code.isdigit() and len(code) == DIGITS


def now_utc() -> datetime:
    return datetime.now(UTC)
