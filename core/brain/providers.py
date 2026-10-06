"""LLM provider abstraction (spec §32).

Providers are replaceable: ``local`` needs no key and degrades honestly,
``openai`` talks to any OpenAI-compatible endpoint. Adding Anthropic or a
local Ollama model means adding one class here and one case in
``get_provider`` — the orchestrator does not change.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from config.settings import settings


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str = ""


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    ok: bool = True
    error: str = ""


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> Completion:
        """Return a reply, possibly containing tool calls."""

    def available(self) -> tuple[bool, str]:
        return True, ""


class LocalProvider(LLMProvider):
    """No network, no key. Used when no cloud provider is configured.

    It does not pretend to be a language model: it signals ``fallback`` so the
    orchestrator routes to the rule engine instead of inventing prose.
    """

    name = "local"

    def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> Completion:
        return Completion(text="", tool_calls=[], ok=True, error="fallback")


class OpenAIProvider(LLMProvider):
    name = "openai"

    def available(self) -> tuple[bool, str]:
        if not settings.openai_api_key:
            return False, "Integration not configured: set ALYO_OPENAI_API_KEY."
        return True, ""

    def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> Completion:
        payload: dict[str, Any] = {
            "model": settings.openai_model,
            "messages": messages,
            "temperature": 0.4,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            response = httpx.post(
                f"{settings.openai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json=payload,
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            return Completion(ok=False, error=f"LLM request failed: {exc.__class__.__name__}")
        except ValueError:
            return Completion(ok=False, error="LLM returned an unreadable response.")

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args, call_id=tc.get("id", "")))
        return Completion(text=message.get("content") or "", tool_calls=calls)


def get_provider(name: str | None = None) -> LLMProvider:
    chosen = (name or settings.llm_provider).lower()
    if chosen == "openai":
        provider = OpenAIProvider()
        ok, reason = provider.available()
        if not ok:
            return LocalProvider()
        return provider
    return LocalProvider()


def provider_status() -> dict[str, Any]:
    """Honest status report for the health endpoint."""
    chosen = settings.llm_provider.lower()
    if chosen == "openai":
        provider = OpenAIProvider()
        ok, reason = provider.available()
        return {
            "provider": "openai",
            "model": settings.openai_model,
            "available": ok,
            "detail": reason or "Configured and ready.",
            "mode": "cloud" if ok else "degraded-to-local",
        }
    return {
        "provider": "local",
        "model": None,
        "available": True,
        "detail": "Rule-based local brain. Set ALYO_LLM_PROVIDER=openai plus an API key for cloud AI.",
        "mode": "local-rules",
    }
