"""Workspace-specific PII anonymization for health mode."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.health.storage import HealthWorkspace
from nanobot.providers.base import LLMResponse, ToolCallRequest


@dataclass(frozen=True)
class IdentifierToken:
    token: str
    raw_value: str


class PIIAnonymizer:
    """Replace raw vault identifiers with reversible tokens."""

    _TOKEN_PREFIXES = {
        "person_names": "PERSON",
        "emails": "EMAIL",
        "phones": "PHONE",
        "chat_ids": "CHAT",
        "channels": "CHANNEL",
    }

    def __init__(self, workspace: Path, *, secret: str | None = None):
        self.workspace = workspace
        self.secret = secret
        self.health = HealthWorkspace(workspace)

    def _load_identifier_tokens(self) -> list[IdentifierToken]:
        vault = self.health.load_vault(secret=self.secret) or {}
        identifiers = vault.get("identifiers") or {}
        tokens: list[IdentifierToken] = []
        for key, prefix in self._TOKEN_PREFIXES.items():
            values = identifiers.get(key) or []
            for idx, raw in enumerate(values, start=1):
                value = str(raw).strip()
                if not value:
                    continue
                tokens.append(IdentifierToken(f"[{prefix}_{idx:03d}]", value))
        tokens.sort(key=lambda item: len(item.raw_value), reverse=True)
        return tokens

    @staticmethod
    def _compile_value_pattern(value: str) -> re.Pattern[str]:
        escaped = re.escape(value)
        if re.fullmatch(r"[\w .'-]+", value):
            return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)
        return re.compile(escaped, re.IGNORECASE)

    def tokenize(self, text: str, token_map: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
        if not text:
            return text, token_map or {}
        current_map = dict(token_map or {})
        result = text
        for identifier in self._load_identifier_tokens():
            pattern = self._compile_value_pattern(identifier.raw_value)
            updated = pattern.sub(identifier.token, result)
            if updated != result:
                current_map[identifier.token] = identifier.raw_value
                result = updated
        return result, current_map

    def detokenize(self, text: str, token_map: dict[str, str]) -> str:
        if not text:
            return text
        result = text
        for token, raw in sorted(token_map.items(), key=lambda item: len(item[0]), reverse=True):
            result = result.replace(token, raw)
        return result

    def _transform(self, value: Any, fn) -> Any:
        if isinstance(value, str):
            return fn(value)
        if isinstance(value, list):
            return [self._transform(item, fn) for item in value]
        if isinstance(value, dict):
            return {key: self._transform(item, fn) for key, item in value.items()}
        return value

    def anonymize_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        token_map: dict[str, str] = {}

        def _apply(text: str) -> str:
            nonlocal token_map
            anonymized, token_map = self.tokenize(text, token_map)
            return anonymized

        sanitized: list[dict[str, Any]] = []
        for message in messages:
            cloned = copy.deepcopy(message)
            for key in ("content", "reasoning_content", "thinking_blocks"):
                if key in cloned:
                    cloned[key] = self._transform(cloned[key], _apply)
            if cloned.get("tool_calls"):
                tool_calls = []
                for tool_call in cloned["tool_calls"]:
                    tool_cloned = copy.deepcopy(tool_call)
                    if isinstance(tool_cloned, dict):
                        if "arguments" in tool_cloned:
                            tool_cloned["arguments"] = self._transform(
                                tool_cloned["arguments"], _apply
                            )
                        elif isinstance(tool_cloned.get("function"), dict) and "arguments" in tool_cloned["function"]:
                            tool_cloned["function"]["arguments"] = self._transform(
                                tool_cloned["function"]["arguments"], _apply
                            )
                    tool_calls.append(tool_cloned)
                cloned["tool_calls"] = tool_calls
            sanitized.append(cloned)
        return sanitized, token_map

    def deanonymize_response(self, response: LLMResponse, token_map: dict[str, str]) -> LLMResponse:
        tool_calls = [
            ToolCallRequest(
                id=tool_call.id,
                name=tool_call.name,
                arguments=self._transform(tool_call.arguments, lambda text: self.detokenize(text, token_map)),
                extra_content=self._transform(tool_call.extra_content, lambda text: self.detokenize(text, token_map)),
                provider_specific_fields=tool_call.provider_specific_fields,
                function_provider_specific_fields=tool_call.function_provider_specific_fields,
            )
            for tool_call in response.tool_calls
        ]
        return LLMResponse(
            content=self._transform(response.content, lambda text: self.detokenize(text, token_map)),
            tool_calls=tool_calls,
            finish_reason=response.finish_reason,
            usage=dict(response.usage or {}),
            retry_after=response.retry_after,
            reasoning_content=self._transform(
                response.reasoning_content,
                lambda text: self.detokenize(text, token_map),
            ),
            thinking_blocks=self._transform(
                response.thinking_blocks,
                lambda text: self.detokenize(text, token_map),
            ),
        )
