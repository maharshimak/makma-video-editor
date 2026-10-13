"""Provider-agnostic model gateway.

Business logic never imports a vendor SDK. It asks the router for a
capability -- reason, vision, embed, transcribe, segment, generate_image,
generate_video -- and the router picks a provider from the registry. Swapping
the underlying model is configuration, not a rewrite, which matters because
every one of today's best models will be replaced.
"""
from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..config import settings

log = logging.getLogger("makma.ai.router")


class Capability(str, Enum):
    REASON = "reason"
    VISION = "vision"
    EMBED = "embed"
    TRANSCRIBE = "transcribe"
    SEGMENT = "segment"
    DETECT = "detect"
    GENERATE_IMAGE = "generate_image"
    GENERATE_VIDEO = "generate_video"


class QualityMode(str, Enum):
    FAST = "fast"
    BALANCED = "balanced"
    MAX = "max"
    LOCAL_ONLY = "local_only"


@dataclass
class Usage:
    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0
    #: False when no price is known for this model. The caller must report
    #: that as unknown, never as free.
    cost_known: bool = True
    cache_hit: bool = False


@dataclass
class ModelResponse:
    text: str = ""
    data: Any = None
    usage: Usage = field(default_factory=Usage)
    ok: bool = True
    error: str = ""


class NotConfigured(RuntimeError):
    """Raised when a capability has no provider that is actually usable."""


class Provider(abc.ABC):
    name: str = "provider"
    capabilities: set[Capability] = set()

    @abc.abstractmethod
    def available(self) -> bool:
        """Whether this provider can serve a request right now."""

    def describe(self) -> dict:
        return {
            "name": self.name,
            "capabilities": sorted(c.value for c in self.capabilities),
            "available": self.available(),
        }

    def reason(self, system: str, messages: list[dict], tools: list[dict] | None = None,
               max_tokens: int = 4096, temperature: float = 0.2) -> ModelResponse:
        raise NotImplementedError

    def vision(self, prompt: str, images: list[bytes], **kw) -> ModelResponse:
        raise NotImplementedError

    def embed(self, texts: list[str]) -> ModelResponse:
        raise NotImplementedError

    def transcribe(self, audio_path: str, **kw) -> ModelResponse:
        raise NotImplementedError


class ModelRouter:
    """Chooses a provider per capability, records usage, and degrades honestly.

    If nothing can serve a capability the router raises `NotConfigured` rather
    than returning a plausible-looking fake, so the UI can say what is actually
    missing.
    """

    def __init__(self) -> None:
        self._providers: list[Provider] = []
        self._usage_sink = None

    def register(self, provider: Provider) -> None:
        self._providers.append(provider)

    def set_usage_sink(self, sink) -> None:
        """Callable receiving (capability, Usage, ok) for cost accounting."""
        self._usage_sink = sink

    def providers_for(self, capability: Capability) -> list[Provider]:
        return [
            p for p in self._providers
            if capability in p.capabilities and p.available()
        ]

    def has(self, capability: Capability) -> bool:
        return bool(self.providers_for(capability))

    def pick(self, capability: Capability) -> Provider:
        options = self.providers_for(capability)
        if not options:
            raise NotConfigured(
                f"No provider is configured for '{capability.value}'. "
                "Set MAKMA_ANTHROPIC_API_KEY (or MAKMA_OPENAI_API_KEY with "
                "MAKMA_OPENAI_BASE_URL) to enable it."
            )
        return options[0]

    def reason(self, system: str, messages: list[dict],
               tools: list[dict] | None = None, max_tokens: int = 4096,
               temperature: float = 0.2, project_id: str = "") -> ModelResponse:
        provider = self.pick(Capability.REASON)
        started = time.time()
        try:
            resp = provider.reason(system, messages, tools, max_tokens, temperature)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            log.warning("provider %s failed: %s", provider.name, exc)
            resp = ModelResponse(ok=False, error=str(exc))
            resp.usage.provider = provider.name
        resp.usage.duration_ms = int((time.time() - started) * 1000)
        self._record(Capability.REASON, resp, project_id)
        return resp

    def _record(self, capability: Capability, resp: ModelResponse,
                project_id: str) -> None:
        if self._usage_sink is None:
            return
        try:
            self._usage_sink(capability.value, resp.usage, resp.ok, project_id)
        except Exception:  # pragma: no cover - accounting must never break a call
            log.exception("usage sink failed")

    def describe(self) -> dict:
        return {
            "providers": [p.describe() for p in self._providers],
            "capabilities": {
                c.value: [p.name for p in self.providers_for(c)] for c in Capability
            },
        }


_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    global _router
    if _router is None:
        from .providers.anthropic_provider import AnthropicProvider
        from .providers.openai_provider import OpenAICompatibleProvider

        _router = ModelRouter()
        _router.register(AnthropicProvider())
        _router.register(OpenAICompatibleProvider())
    return _router


def reset_router() -> None:
    """Used by tests and by settings changes."""
    global _router
    _router = None
