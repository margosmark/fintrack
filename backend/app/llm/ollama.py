"""Ollama adapter — OpenAI-compatible endpoint (docs/04).

Defaults: http://localhost:11434/v1, model qwen2.5:7b-instruct.
No API key required for a local Ollama; empty base_url falls back to default.
"""

from app.llm.base import post_chat_completions

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen2.5:7b-instruct"


class OllamaProvider:
    def __init__(
        self,
        base_url: str = "",
        api_key: str | None = None,
        model: str = "",
    ) -> None:
        # post_chat_completions appends /chat/completions, so Ollama's
        # OpenAI-compatible base must end in /v1. Tolerate users typing the
        # host root, a trailing slash, or even the full /chat/completions path.
        raw = (base_url or DEFAULT_BASE_URL).rstrip("/")
        if raw.endswith("/chat/completions"):
            raw = raw[: -len("/chat/completions")].rstrip("/")
        if not raw.endswith("/v1"):
            raw = raw + "/v1"
        self.base_url = raw
        self.api_key = api_key or None
        if not model:
            raise ValueError("OllamaProvider requires a model name")
        self.model = model

    def chat(self, messages: list[dict], json_schema: dict) -> dict:
        return post_chat_completions(self.base_url, self.api_key, self.model, messages, json_schema)
