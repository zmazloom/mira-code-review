"""Adapter for the company's Anthropic Messages-compatible endpoint."""

from __future__ import annotations

import json
import logging
import os
from typing import ClassVar, NoReturn

import httpx

from mira.config import COMPANY_MESSAGES_URL, LLMConfig
from mira.exceptions import LLMError, NonRetriableLLMError, OutputLimitLLMError
from mira.llm.base import OpenAICompatibleProvider
from mira.observability import event, http_request

logger = logging.getLogger(__name__)

_MODEL = "developer-model"
_MAX_TOKENS = 100_000


def _messages_input(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Convert Mira's chat-shaped messages to the Messages API shape."""
    system_parts: list[str] = []
    converted: list[dict] = []

    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")

        if role == "system":
            if content:
                system_parts.append(str(content))
            continue

        # Anthropic-compatible thinking-enabled tool turns must be replayed
        # verbatim, including signatures. Never reconstruct their reasoning.
        if role == "assistant" and isinstance(message.get("_company_content"), list):
            converted.append({"role": role, "content": message["_company_content"]})
            continue

        if role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id", "call_0"),
                            "content": str(content),
                        }
                    ],
                }
            )
            continue

        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict] = []
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for tool_call in message["tool_calls"]:
                function = tool_call.get("function", {})
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_call.get("id", "call_0"),
                        "name": function.get("name", ""),
                        "input": arguments,
                    }
                )
            converted.append({"role": "assistant", "content": blocks})
            continue

        converted.append({"role": role, "content": content})

    return ("\n\n".join(system_parts) or None), converted


def _messages_tool(tool: dict) -> dict:
    """Convert an OpenAI function tool to the Messages API tool shape."""
    function = tool.get("function", {})
    return {
        "name": function.get("name", ""),
        "description": function.get("description", ""),
        "input_schema": function.get("parameters", {"type": "object"}),
    }


def _text_content(data: dict) -> str:
    content = data.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


class CompanyMessagesProvider(OpenAICompatibleProvider):
    """Provider used only for the exact internal company endpoint."""

    supports_json_mode: ClassVar[bool] = False
    supports_tool_calling: ClassVar[bool] = True

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self._url = COMPANY_MESSAGES_URL

    def _build_headers(self) -> dict[str, str]:
        """Use no auth by default; honor an explicitly configured API key env."""
        headers = {"Content-Type": "application/json"}
        if self.config.api_key_env:
            key = os.environ.get(self.config.api_key_env, "")
            if key and self.config.api_key_env != "OPENROUTER_API_KEY":
                headers["Authorization"] = f"Bearer {key}"
        return headers

    def _body(self, messages: list[dict], max_tokens: int | None = None) -> dict:
        system, converted = _messages_input(messages)
        requested_max = max_tokens if max_tokens is not None else self.config.max_tokens
        body: dict = {
            "model": _MODEL,
            "max_tokens": min(requested_max, _MAX_TOKENS),
            "messages": converted,
            # Do not let gateway-default thinking consume the entire output
            # allowance. Forced tool submissions also require thinking off.
            "thinking": {"type": "disabled"},
        }
        if system:
            body["system"] = system
        return body

    async def _post(self, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.config.request_timeout) as client:
            response = await http_request(
                client,
                "post",
                "llm",
                self._url,
                headers=self._build_headers(),
                json=body,
            )
            self._handle_error(response)
            try:
                data = response.json()
            except ValueError:
                self._invalid_response("Response body is not JSON")
            if not isinstance(data, dict):
                self._invalid_response("Response body is not an object")
            self._account_usage(data)
            content = data.get("content")
            event(
                "llm.response_received",
                "Model response decoded",
                stop_reason=data.get("stop_reason"),
                block_types=[part.get("type") for part in content if isinstance(part, dict)]
                if isinstance(content, list)
                else [],
                usage={
                    "input": (data.get("usage") or {}).get("input_tokens", 0),
                    "output": (data.get("usage") or {}).get("output_tokens", 0),
                },
            )
            if data.get("stop_reason") == "max_tokens":
                reason = "Output limit reached before final submission"
                event("llm.response_invalid", reason, level="WARNING")
                raise OutputLimitLLMError("invalid_response", reason=reason)
            if not isinstance(content, (list, str)):
                self._invalid_response("Response is missing content blocks")
            return data

    @staticmethod
    def _invalid_response(reason: str) -> NoReturn:
        event("llm.response_invalid", reason, level="WARNING")
        raise NonRetriableLLMError("invalid_response", reason=reason)

    def _account_usage(self, data: dict) -> None:
        usage = data.get("usage") or {}
        self.total_prompt_tokens += usage.get("input_tokens", 0)
        self.total_completion_tokens += usage.get("output_tokens", 0)

    async def _call_llm(
        self,
        model: str,
        messages: list[dict[str, str]],
        json_mode: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        data = await self._post(self._body(messages, max_tokens))
        text = _text_content(data)
        if not text:
            self._invalid_response("Response contains no final text")
        return text

    async def _call_llm_with_tools(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
    ) -> str:
        if not tools:
            raise LLMError("no_tools")
        body = self._body(messages)
        body["tools"] = [_messages_tool(tool) for tool in tools]
        body["tool_choice"] = {"type": "tool", "name": tools[0]["function"]["name"]}
        expected = tools[0]["function"]["name"]
        # A missing submission gets one explicit repair, never the transport
        # retry allowance (which may repeat a multi-minute generation five times).
        for repair in range(2):
            data = await self._post(body)
            content = data.get("content", [])
            for part in content if isinstance(content, list) else []:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "tool_use"
                    and part.get("name") == expected
                    and isinstance(part.get("input"), dict)
                ):
                    return json.dumps(part["input"], ensure_ascii=False)
            text = _text_content(data)
            if text:
                logger.warning(
                    "Model returned content instead of tool use, using content as fallback"
                )
                return text
            if repair == 0:
                event("llm.response_repair", "Missing tool submission; requesting one repair")
                body["messages"] = [
                    *body["messages"],
                    {"role": "user", "content": f"Submit the result now using {expected}."},
                ]
        return self._invalid_response("Response contains no valid requested tool submission")

    async def _call_llm_agentic(
        self,
        model: str,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        if not tools:
            raise LLMError("no_tools")
        body = self._body(messages)
        body["tools"] = [_messages_tool(tool) for tool in tools]
        body["tool_choice"] = {"type": "auto"}
        effort = self.config.reasoning_effort
        if effort and effort != "off" and body["max_tokens"] >= 2048:
            # Investigation may opt into bounded thinking, but leave at least
            # half the output capacity for a usable tool call or final answer.
            budget = {"low": 1024, "medium": 2048, "high": 4096}.get(effort, 2048)
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": min(budget, body["max_tokens"] // 2),
            }
        data = await self._post(body)

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        content = data.get("content", [])
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": part.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": part.get("name", ""),
                            "arguments": json.dumps(part.get("input", {}), ensure_ascii=False),
                        },
                    }
                )
        if not tool_calls and not text_parts:
            self._invalid_response("Agentic response contains no text or tool call")
        result = {
            "role": "assistant",
            "content": "".join(text_parts) or None if tool_calls else "".join(text_parts),
            "tool_calls": tool_calls,
        }
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") in ("thinking", "redacted_thinking")
            for part in content
        ):
            result["_company_content"] = content
        return result
