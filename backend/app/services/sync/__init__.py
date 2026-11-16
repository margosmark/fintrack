"""Bank-sync seam (docs/39) — the one place a provider adapter may be plugged in.

Importing this package registers the built-in stub provider, so
`GET /sync/providers` is never empty and the accredited adapter only has to
call `register_provider` once (docs/39 § Adoption is a config change).
"""

from app.services.sync.provider import (
    Provider,
    ProviderAuthRequired,
    ProviderError,
    ProviderRevoked,
    ProviderUnavailable,
    get_provider,
    list_providers,
    register_provider,
)
from app.services.sync.stub import StubProvider, default_stub

register_provider(default_stub())

__all__ = [
    "Provider",
    "ProviderAuthRequired",
    "ProviderError",
    "ProviderRevoked",
    "ProviderUnavailable",
    "StubProvider",
    "default_stub",
    "get_provider",
    "list_providers",
    "register_provider",
]
