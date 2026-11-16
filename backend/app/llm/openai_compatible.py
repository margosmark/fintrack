"""OpenAI-compatible adapter — configurable base_url/api_key/model (docs/04).

Covers OpenAI, OpenRouter, Anthropic-compatible gateways and any endpoint
speaking the /chat/completions wire format. A missing base_url is a
construction failure (degrades to "no LLM calls this run").
"""

from app.llm.base import post_chat_completions


class OpenAiCompatibleProvider:
    def __init__(
        self,
        base_url: str = "",
        api_key: str | None = None,
        model: str = "",
    ) -> None:
        if not base_url:
            raise ValueError("OpenAiCompatibleProvider requires base_url")
        if not model:
            raise ValueError("OpenAiCompatibleProvider requires a model name")
        self.base_url = base_url
        self.api_key = api_key or None
        self.model = model

    def chat(self, messages: list[dict], json_schema: dict) -> dict:
        return post_chat_completions(self.base_url, self.api_key, self.model, messages, json_schema)
