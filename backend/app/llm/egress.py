"""Central LLM egress host policy + privacy lock (audit H1/H3; docs/11 step 5).

Every outbound LLM call funnels through `app.llm.base.post_chat_completions`,
which calls `assert_send_allowed` here BEFORE any socket is opened:

- scheme must be ``http`` or ``https`` — nothing else, operator host or not;
- **privacy mode** (the ContextVar bound per-request in ``app.api.deps`` and
  per-run in ``build_provider``) permits ONLY loopback targets. Checked first,
  with no exemptions and regardless of the ``llm_provider`` enum (H3);
- otherwise every A/AAAA record of the hostname is resolved and denied when it
  lands in RFC1918 / link-local / CGNAT / ULA / unspecified space, and plain
  ``http`` is only accepted for loopback targets — UNLESS the hostname equals
  the operator env ``LLM_BASE_URL`` host (trusted) or
  ``settings.llm_allow_private_hosts`` is on. Loopback is always allowed;
- the caller never follows redirects (``follow_redirects=False`` at the send
  point), so a public host cannot bounce the request at an internal one.

DNS-rebinding residual (accepted, documented): addresses are re-resolved at
send time and the check runs microseconds before httpx' own lookup. Full IP
pinning would need a custom transport and is out of scope for this wave. An
UNRESOLVABLE hostname is tolerated at PATCH time (nothing to deny yet — the
send seam re-checks) but refused at send time, so httpx never races a second
lookup for a target we could not verify.

Key routing (H1): the operator env ``LLM_API_KEY`` is attached only to the
operator's own env ``LLM_BASE_URL`` destination — see `select_api_key`.

Diagnostics policy: callers log upstream status/URL through `safe_target`
(server-side stdlib logging only — no userinfo, query string, prompt content
or keys) and surface the generic ``llm_upstream_error`` envelope to clients.
"""

import ipaddress
import logging
import socket
from contextvars import ContextVar
from urllib.parse import urlsplit

from app.core.config import Settings, get_settings
from app.core.errors import APIError, api_error

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = ("http", "https")

# Exact deny list (audit H1). NOTE: deliberately NOT `ip.is_private` — modern
# CPython counts documentation ranges (TEST-NET-1/2/3) as private, which would
# reject legitimate public-looking targets beyond the audited scope.
_DENIED_V4 = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",  # RFC1918
        "172.16.0.0/12",  # RFC1918
        "192.168.0.0/16",  # RFC1918
        "169.254.0.0/16",  # link-local (cloud metadata 169.254.169.254)
        "100.64.0.0/10",  # CGNAT (RFC6598)
        "0.0.0.0/8",  # "this network" / unspecified
    )
)
_DENIED_V6 = tuple(
    ipaddress.ip_network(net)
    for net in (
        "fe80::/10",  # link-local
        "fc00::/7",  # ULA
    )
)

# Privacy lock rides the request/run context exactly like the fx/market_data
# ContextVars (docs/11 step 6): task-scoped, so concurrent requests cannot see
# each other's flag.
_privacy_lock: ContextVar[bool] = ContextVar("llm_privacy_lock", default=False)


def bind_privacy_lock(enabled: bool) -> None:
    """Bind the requesting user's privacy_mode to this request/run context."""
    _privacy_lock.set(enabled)


def privacy_locked() -> bool:
    """True when the active context must only talk to loopback LLM endpoints."""
    return _privacy_lock.get()


def safe_target(url: str) -> str:
    """Scheme://host[:port]path with userinfo/query/fragment stripped.

    Server-side log form for an upstream target: the path is enough to debug,
    and a credential smuggled into the URL (key-in-query, basic-auth userinfo)
    can never reach the log.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        try:
            port = parts.port
        except ValueError:
            port = None
    except ValueError:
        return "<unparseable-url>"
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path}"


def _url_host(url: str) -> str | None:
    """Lowercased hostname of `url`, or None when it carries none/is invalid."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.lower().rstrip(".")


def operator_host(settings: Settings) -> str | None:
    """Host of the operator env LLM_BASE_URL — the one trusted destination."""
    return _url_host((settings.llm_base_url or "").strip())


def select_api_key(
    base_url: str, user_key: str | None = None, settings: Settings | None = None
) -> str | None:
    """H1: the operator env LLM_API_KEY may only reach the operator's own host.

    Resolution: the user's own key (stored or candidate) always wins; the env
    key is attached only when the effective destination is the operator's env
    ``LLM_BASE_URL`` (an unset base URL means "operator default" — the
    provider's own fallback, never a user-chosen host); otherwise no key at
    all. A user-supplied endpoint therefore CANNOT exfiltrate the env key.
    """
    if user_key:
        return user_key
    settings = settings or get_settings()
    if not settings.llm_api_key:
        return None
    effective = (base_url or "").strip() or (settings.llm_base_url or "").strip()
    if not effective:
        # Nothing configured anywhere: provider default destination (local
        # Ollama), i.e. the operator's own machine — env key stays home.
        return settings.llm_api_key
    host = _url_host(effective)
    trusted = operator_host(settings)
    if host is not None and trusted is not None and host == trusted:
        return settings.llm_api_key
    return None


def _resolve(host: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address] | None:
    """All A/AAAA addresses for `host`; None when it cannot be verified."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    addrs: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]  # strip IPv6 zone id
        try:
            addrs.add(ipaddress.ip_address(raw))
        except ValueError:
            return None
    return addrs or None


def _denied(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for every address the policy refuses (loopback handled by caller)."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped  # ::ffff:192.168.0.1 must not slip through
    if addr.is_loopback:
        return False
    if addr.is_unspecified:
        return True
    nets = _DENIED_V4 if addr.version == 4 else _DENIED_V6
    return any(addr in net for net in nets)


def _policy_error() -> APIError:
    # Fixed message: no resolved addresses, no upstream detail — the caller
    # supplied this URL, but a hostname probe must not become a DNS oracle.
    return api_error(422, "llm_base_url target is not allowed", "invalid_llm_base_url")


def _privacy_error(at_send: bool) -> APIError:
    if at_send:
        # Same family as the fx/market_data seams (docs/11 step 6).
        return api_error(
            403,
            "Privacy mode allows only loopback LLM endpoints; external calls are blocked",
            "privacy_mode_blocks_external_api",
        )
    # PATCH write time pins 422 invalid_llm_base_url (slice contract).
    return api_error(
        422,
        "privacy_mode=true allows only a loopback llm_base_url",
        "invalid_llm_base_url",
    )


def _check(url: str, *, privacy: bool, settings: Settings, at_send: bool) -> None:
    # 1. Structure: http/https with a hostname — unconditional (H1).
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        raw_host = parts.hostname
    except ValueError:
        raise _policy_error() from None
    if scheme not in _ALLOWED_SCHEMES or not raw_host:
        raise _policy_error()
    host = raw_host.lower().rstrip(".")

    exempt = host == operator_host(settings) or bool(settings.llm_allow_private_hosts)

    # 2. Privacy lock first, no exemptions: only provably loopback targets
    #    may be contacted while privacy_mode is on (H3).
    if privacy:
        addrs = _resolve(host)
        if not addrs or not all(addr.is_loopback for addr in addrs):
            raise _privacy_error(at_send)
        return

    # 3. Operator host / LLM_ALLOW_PRIVATE_HOSTS: trusted, no DNS needed.
    if exempt:
        return

    # 4. Verify every resolved address; unverifiable targets fail closed at
    #    the send point but are storable at write time (the send seam re-checks).
    addrs = _resolve(host)
    if not addrs:
        if at_send:
            raise _policy_error()
        return
    if scheme == "http" and not all(addr.is_loopback for addr in addrs):
        raise _policy_error()  # plain http is for loopback targets only
    for addr in addrs:
        if _denied(addr):
            raise _policy_error()


def validate_base_url(url: str, *, privacy: bool, settings: Settings | None = None) -> None:
    """PATCH write-time validation (422 ``invalid_llm_base_url``).

    `privacy` is the EFFECTIVE privacy mode after the patch: under privacy
    mode only a loopback base URL may be stored. The send seam re-runs the
    same policy on every request, so a row stored before a policy change
    still cannot leave the machine.
    """
    settings = settings or get_settings()
    _check(url, privacy=privacy, settings=settings, at_send=False)


def assert_send_allowed(url: str, *, settings: Settings | None = None) -> None:
    """The single send-point guard — raises before any I/O happens.

    Privacy violations surface as 403 ``privacy_mode_blocks_external_api``;
    host-policy violations as 422 ``invalid_llm_base_url``. Callers translate
    upstream transport/status failures separately into the generic
    502 ``llm_upstream_error`` envelope.
    """
    settings = settings or get_settings()
    _check(url, privacy=_privacy_lock.get(), settings=settings, at_send=True)
