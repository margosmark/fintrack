"""Vault envelope schemas — wrapped-DEK sync (E2EE Phase 4, docs/33).

`wrapped_blob` is opaque: these models validate SIZE and SHAPE only, never
content. `extra="forbid"` everywhere so a typo'd field is a loud 422 instead of
a silently ignored one (docs/08 § Vault key sync).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class VaultEnvelopeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=32, max_length=64, pattern=r"^[0-9a-f]{32}$")
    label: str | None = Field(default=None, max_length=64)
    wrapped_blob: str


class VaultEnvelopeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No max_length here: oversize is a domain error (422 envelope_too_large),
    # raised in the handler like the Phase-2 invalid_cipher check.
    wrapped_blob: str
    label: str | None = Field(default=None, max_length=64)


class VaultEnvelopePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    label: str | None
    wrapped_blob: str | None
    revoked_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


class VaultEnvelopeList(BaseModel):
    items: list[VaultEnvelopePublic]
    revoked_own_device: bool
    active_count: int
