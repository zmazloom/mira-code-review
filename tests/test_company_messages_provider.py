"""Tests for the exact company Messages endpoint adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import COMPANY_MESSAGES_URL, LLMConfig
from mira.llm import create_llm
from mira.llm.company_messages import CompanyMessagesProvider


def _response(data: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = data
    return response


@pytest.mark.asyncio
async def test_company_endpoint_uses_messages_body_and_response():
    provider = CompanyMessagesProvider(LLMConfig(base_url=COMPANY_MESSAGES_URL, max_tokens=16_000))
    response = _response(
        {
            "content": [{"type": "text", "text": "سلام"}],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
    )
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("mira.llm.company_messages.httpx.AsyncClient", return_value=client):
        result = await provider.complete(
            [
                {"role": "system", "content": "پاسخ بده"},
                {"role": "user", "content": "سلام"},
            ],
            json_mode=True,
        )

    assert result == "سلام"
    request = client.post.call_args
    assert request.args[0] == COMPANY_MESSAGES_URL
    body = request.kwargs["json"]
    assert body == {
        "model": "developer-model",
        "max_tokens": 16_000,
        "messages": [{"role": "user", "content": "سلام"}],
        "system": "پاسخ بده",
        "thinking": {"type": "disabled"},
    }
    assert provider.usage["prompt_tokens"] == 4
    assert provider.usage["completion_tokens"] == 2


def test_company_endpoint_caps_configured_output_at_endpoint_limit():
    provider = CompanyMessagesProvider(LLMConfig(base_url=COMPANY_MESSAGES_URL, max_tokens=150_000))
    assert provider._body([{"role": "user", "content": "hello"}])["max_tokens"] == 100_000


def test_factory_uses_company_adapter_only_for_company_url():
    company = create_llm(LLMConfig(base_url=COMPANY_MESSAGES_URL))
    other = create_llm(LLMConfig(base_url="https://api.example.com/v1"))
    assert isinstance(company, CompanyMessagesProvider)
    assert type(other).__name__ == "LLMProvider"


def test_company_http_url_is_allowed_but_other_public_http_is_rejected():
    LLMConfig(base_url=COMPANY_MESSAGES_URL)
    with pytest.raises(ValueError):
        LLMConfig(base_url="http://api.example.com/v1")
