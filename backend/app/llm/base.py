"""LlmProvider protocol + per-run provider resolution (docs/04).

Resolution order: non-null user-row settings override env defaults (docs/02
`users`). Construction failures degrade to "no LLM calls this run" — the
caller logs and skips layer 3 while layers 1-2 are unaffected (w3 contract 8).

`post_chat_completions` is THE single outbound LLM send in the app: the
central egress policy in `app/llm/egress.py` (scheme/host/privacy checks, H1
key routing) runs there before any I/O, redirects are never followed, and
upstream failures become the generic 502 `llm_upstream_error` envelope with
status/URL logged server-side only (audit H1/H3, docs/11 step 5).
"""

import json
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import Settings, get_settings
from app.core.crypto import decrypt_secret
from app.core.errors import api_error
from app.llm.egress import (
    assert_send_allowed,
    bind_privacy_lock,
    safe_target,
    select_api_key,
)
from app.models.users import User

logger = logging.getLogger(__name__)

CHAT_TIMEOUT_SECONDS = 60.0


class LlmProvider(Protocol):
    def chat(self, messages: list[dict], json_schema: dict) -> dict: ...


@dataclass
class ResolvedLlm:
    """Provider ready for this run; `provider is None` means degrade to no-op."""

    provider: LlmProvider | None
    model: str


def post_chat_completions(
    base_url: str,
    api_key: str | None,
    model: str,
    messages: list[dict],
    json_schema: dict,
) -> dict:
    """Shared OpenAI-compatible wire call; returns the parsed JSON content dict.

    The single send point (audit H1/H3):
    - `assert_send_allowed` enforces scheme/host policy AND the privacy lock
      first — a blocked destination raises before any socket is opened;
    - redirects are never followed (a public host must not bounce us at an
      internal one);
    - every upstream failure (transport error, non-2xx/3xx, unparseable body)
      is logged server-side via stdlib logging (status + sanitized target —
      never prompt content or keys) and raised as the generic 502
      `llm_upstream_error` envelope: the client never sees the upstream
      status, URL or transport message.

    Raises `APIError` (egress policy or upstream failure) and never leaks
    `httpx` errors to callers. ValueError is no longer raised from here —
    a malformed assistant payload is an upstream failure like any other.
    """
    assert_send_allowed(base_url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "categorization", "schema": json_schema, "strict": True},
        },
    }
    target = f"{base_url.rstrip('/')}/chat/completions"
    try:
        response = httpx.post(
            target,
            json=payload,
            headers=headers,
            timeout=CHAT_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "LLM upstream request to %s failed (%s)",
            safe_target(target),
            type(exc).__name__,
        )
        raise api_error(502, "LLM upstream request failed", "llm_upstream_error") from exc
    if not response.is_success:
        logger.warning(
            "LLM upstream %s returned HTTP %s", safe_target(target), response.status_code
        )
        raise api_error(502, "LLM upstream request failed", "llm_upstream_error")
    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning(
            "LLM upstream %s returned an unexpected payload (%s)",
            safe_target(target),
            type(exc).__name__,
        )
        raise api_error(502, "LLM upstream request failed", "llm_upstream_error") from exc
    return parsed


def resolve_settings(
    user: User, settings: Settings | None = None
) -> tuple[str, str, str | None, str]:
    """(kind, base_url, api_key, model) with user-row values overriding env.

    Key routing (H1): the user's own stored key if any; otherwise the operator
    env LLM_API_KEY ONLY when the effective destination is the operator's own
    env LLM_BASE_URL (`select_api_key`) — a user-supplied endpoint can never
    receive the operator's key.
    """

    settings = settings or get_settings()
    kind = user.llm_provider.value if user.llm_provider else settings.llm_provider
    base_url = user.llm_base_url or settings.llm_base_url
    stored_key = decrypt_secret(user.llm_api_key) if user.llm_api_key else None
    api_key = select_api_key(base_url, stored_key, settings)
    model = user.llm_model or settings.llm_model
    return kind, base_url, api_key, model


def make_provider(kind: str, base_url: str, api_key: str | None, model: str) -> LlmProvider | None:
    """Construct a concrete adapter; any failure degrades to None (no LLM this run)."""
    from app.llm.ollama import OllamaProvider
    from app.llm.openai_compatible import OpenAiCompatibleProvider

    kind = str(kind)
    try:
        if kind == "ollama":
            return OllamaProvider(base_url=base_url, api_key=api_key, model=model)
        return OpenAiCompatibleProvider(base_url=base_url, api_key=api_key, model=model)
    except Exception:
        logger.warning("LLM provider %s could not be constructed; skipping LLM layer", kind)
        return None


def build_provider(user: User, settings: Settings | None = None) -> ResolvedLlm:
    """Construct the concrete adapter for this run.

    Never raises: any construction failure degrades to provider=None so the
    run proceeds without LLM calls.

    Also publishes the user's privacy_mode to the egress seam's ContextVar
    (H3): background workers (categorization layer 3) and any other context
    where `app.api.deps` never bound the lock inherit the loopback-only rule
    as long as they got the provider through here — which every LLM caller
    outside `POST /users/me/llm-test` does.
    """
    settings = settings or get_settings()
    bind_privacy_lock(bool(user.privacy_mode))
    kind, base_url, api_key, model = resolve_settings(user, settings)
    provider = make_provider(kind, base_url, api_key, model)
    return ResolvedLlm(provider, model)
