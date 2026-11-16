"""`Provider` protocol + registry — the one seam where network may happen (docs/39).

AGENTS §2 pins external network to `services/fx.py` and `services/market_data.py`
(and, per docs/11's egress inventory, `services/notify_delivery.py`). Bank sync
adds destination 6: **only implementations of this protocol may reach a
provider**, and only `services/sync/service.py` may call one. Nothing else in
the codebase imports an implementation.

Nothing here talks to the database or to FastAPI — the service layer does both.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.services.sync.types import (
    AgreementGrant,
    NormalizedBalance,
    NormalizedTx,
    ProviderAccount,
    ProviderInstitution,
    ProviderUser,
    PullBatch,
)


class ProviderError(Exception):
    """Base for "the provider could not complete this call".

    Carries the `{detail, code}` envelope the router will raise, so an adapter
    never invents an error shape. Subclasses narrow the status.
    """

    status_code: int = 502
    code: str = "sync_provider_unavailable"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ProviderUnavailable(ProviderError):
    """Transport / parse / upstream failure — retryable, 502."""

    status_code = 502
    code = "sync_provider_unavailable"


class ProviderAuthRequired(ProviderError):
    """Consent expired, MFA re-prompt, or credentials invalid — 409."""

    status_code = 409
    code = "sync_auth_required"


class ProviderRevoked(ProviderError):
    """The provider no longer honours this agreement — 409."""

    status_code = 409
    code = "sync_agreement_revoked"


@runtime_checkable
class Provider(Protocol):
    """Open-banking provider adapter (docs/39 § Interface).

    Four operations, no I/O contract beyond "may perform network I/O": the
    service layer applies the privacy-mode egress guard before any call.
    """

    slug: str
    display_name: str

    def list_institutions(self) -> Sequence[ProviderInstitution]:
        """Institutions this provider can serve (may be a network call)."""
        ...

    def create_agreement(
        self, user: ProviderUser, accounts: Sequence[ProviderAccount]
    ) -> AgreementGrant:
        """Obtain consent for `accounts` on behalf of `user`."""
        ...

    def pull_delta(self, user: ProviderUser, since: datetime | None) -> PullBatch:
        """Rows newer than `since` (None = full history) for `user`."""
        ...

    def revoke(self, user: ProviderUser) -> None:
        """Invalidate the provider's side of the agreement (idempotent)."""
        ...


_REGISTRY: dict[str, Provider] = {}


def register_provider(provider: Provider) -> Provider:
    """Add/replace a provider by slug. Returns the registered instance."""
    _REGISTRY[provider.slug] = provider
    return provider


def get_provider(slug: str) -> Provider | None:
    return _REGISTRY.get(slug)


def list_providers() -> list[Provider]:
    """Registered providers, stable slug order (drives the future picker UI)."""
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


__all__ = [
    "AgreementGrant",
    "NormalizedBalance",
    "NormalizedTx",
    "Provider",
    "ProviderAccount",
    "ProviderAuthRequired",
    "ProviderError",
    "ProviderInstitution",
    "ProviderRevoked",
    "ProviderUnavailable",
    "ProviderUser",
    "PullBatch",
    "get_provider",
    "list_providers",
    "register_provider",
]
