"""Anthropic provider for the reasoning and vision capabilities."""
from __future__ import annotations

import base64
import logging

from ...config import settings
from ..pricing import estimate
from ..router import Capability, ModelResponse, Provider, Usage

log = logging.getLogger("makma.ai.anthropic")

class AnthropicProvider(Provider):
    name = "anthropic"
    capabilities = {Capability.REASON, Capability.VISION}

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or settings.anthropic_api_key
        self.model = model or settings.director_model
        self._client = None

    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def reason(self, system: str, messages: list[dict],
               tools: list[dict] | None = None, max_tokens: int = 4096,
               temperature: float = 0.2) -> ModelResponse:
        client = self._get_client()
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = {"type": "any"}

        result = client.messages.create(**kwargs)

        text_parts: list[str] = []
        tool_payload = None
        for block in result.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_payload = {"name": block.name, "input": block.input}

        cost, known = estimate(self.model, result.usage.input_tokens,
                               result.usage.output_tokens)
        usage = Usage(
            provider=self.name,
            model=self.model,
            tokens_in=result.usage.input_tokens,
            tokens_out=result.usage.output_tokens,
            cost_usd=cost,
            cost_known=known,
        )
        return ModelResponse(
            text="\n".join(text_parts), data=tool_payload, usage=usage
        )

    def vision(self, prompt: str, images: list[bytes], **kw) -> ModelResponse:
        content: list[dict] = []
        for raw in images[:20]:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(raw).decode(),
                    },
                }
            )
        content.append({"type": "text", "text": prompt})
        return self.reason(
            system=kw.get("system", "You are a precise visual analyst."),
            messages=[{"role": "user", "content": content}],
            max_tokens=kw.get("max_tokens", 2048),
        )
