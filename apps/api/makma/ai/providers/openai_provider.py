"""OpenAI-compatible provider.

Covers OpenAI itself and anything speaking the same HTTP shape -- Ollama,
vLLM, LM Studio, LiteLLM, OpenRouter -- which is what keeps a fully local
deployment possible without touching the rest of the codebase.
"""
from __future__ import annotations

import json
import logging

import httpx

from ...config import settings
from ..pricing import estimate
from ..router import Capability, ModelResponse, Provider, Usage

log = logging.getLogger("makma.ai.openai")


class OpenAICompatibleProvider(Provider):
    name = "openai-compatible"
    capabilities = {Capability.REASON}

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None) -> None:
        self.base_url = (base_url or settings.openai_base_url or "").rstrip("/")
        self.api_key = api_key or settings.openai_api_key
        self.model = model or settings.openai_model

    def available(self) -> bool:
        # A local server (Ollama, vLLM) needs no key; a hosted one needs both.
        return bool(self.base_url) and (bool(self.api_key) or "localhost" in self.base_url
                                        or "127.0.0.1" in self.base_url)

    def reason(self, system: str, messages: list[dict],
               tools: list[dict] | None = None, max_tokens: int = 4096,
               temperature: float = 0.2) -> ModelResponse:
        payload: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
                for t in tools
            ]
            payload["tool_choice"] = "required"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        with httpx.Client(timeout=180) as client:
            resp = client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers
            )
            resp.raise_for_status()
            body = resp.json()

        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message", {})
        tool_payload = None
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            try:
                tool_payload = {
                    "name": fn.get("name", ""),
                    "input": json.loads(fn.get("arguments") or "{}"),
                }
            except json.JSONDecodeError:
                log.warning("provider returned unparseable tool arguments")
            break

        usage_body = body.get("usage") or {}
        tokens_in = int(usage_body.get("prompt_tokens", 0))
        tokens_out = int(usage_body.get("completion_tokens", 0))
        # An OpenAI-compatible base URL can be anything, so the model is often
        # not in the price table. Say so rather than reporting it as free.
        cost, known = estimate(self.model, tokens_in, tokens_out)
        usage = Usage(
            provider=self.name,
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            cost_known=known,
        )
        return ModelResponse(
            text=message.get("content") or "", data=tool_payload, usage=usage
        )
