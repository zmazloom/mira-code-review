"""LLM provider package — factory entry point."""

from __future__ import annotations

from mira.config import LLMConfig, is_company_messages_url
from mira.llm.base import LLMProviderProtocol


def create_llm(config: LLMConfig) -> LLMProviderProtocol:
    """Create the appropriate LLM provider based on config.provider.

    Returns an instance satisfying LLMProviderProtocol.
    """
    from mira.observability import event

    event("model.selected", "Selected language model", model=config.model, provider=config.provider)
    if config.provider == "bedrock":
        from mira.llm.bedrock import BedrockProvider

        return BedrockProvider(config)

    if is_company_messages_url(config.base_url):
        from mira.llm.company_messages import CompanyMessagesProvider

        return CompanyMessagesProvider(config)

    if config.api_style == "responses":
        from mira.llm.responses import ResponsesProvider

        return ResponsesProvider(config)

    # Default: OpenAI-compatible endpoint (OpenRouter, vLLM, Ollama, etc.)
    from mira.llm.provider import LLMProvider

    return LLMProvider(config)
