"""LLM provider abstraction (docs/04)."""

from app.llm.base import LlmProvider, ResolvedLlm, build_provider, post_chat_completions
from app.llm.ollama import OllamaProvider
from app.llm.openai_compatible import OpenAiCompatibleProvider

__all__ = [
    "LlmProvider",
    "OllamaProvider",
    "OpenAiCompatibleProvider",
    "ResolvedLlm",
    "build_provider",
    "post_chat_completions",
]
