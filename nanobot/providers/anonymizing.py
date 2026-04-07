"""Provider decorator that anonymizes health workspace traffic."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.security.anonymizer import PIIAnonymizer


class AnonymizingProvider(LLMProvider):
    """Wrap an LLM provider with reversible vault-based anonymization."""

    def __init__(self, inner: LLMProvider, anonymizer: PIIAnonymizer):
        super().__init__(api_key=inner.api_key, api_base=inner.api_base)
        self._inner = inner
        self._anonymizer = anonymizer
        self.generation = inner.generation

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        anonymized_messages, token_map = self._anonymizer.anonymize_messages(messages)
        response = await self._inner.chat(
            messages=anonymized_messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )
        return self._anonymizer.deanonymize_response(response, token_map)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        anonymized_messages, token_map = self._anonymizer.anonymize_messages(messages)
        raw_buffer = ""
        restored_buffer = ""

        async def _restore_delta(delta: str) -> None:
            nonlocal raw_buffer, restored_buffer
            raw_buffer += delta
            restored = self._anonymizer.detokenize(raw_buffer, token_map)
            incremental = restored[len(restored_buffer):]
            restored_buffer = restored
            if incremental and on_content_delta:
                await on_content_delta(incremental)

        response = await self._inner.chat_stream(
            messages=anonymized_messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
            on_content_delta=_restore_delta if on_content_delta else None,
        )
        return self._anonymizer.deanonymize_response(response, token_map)

    def get_default_model(self) -> str:
        return self._inner.get_default_model()
