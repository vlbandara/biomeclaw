from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.health.storage import HealthWorkspace
from nanobot.providers.anonymizing import AnonymizingProvider
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.security.anonymizer import PIIAnonymizer


class ScriptedProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self.responses = list(responses)
        self.calls = 0
        self.last_messages = None

    async def chat(self, messages, **kwargs) -> LLMResponse:
        self.calls += 1
        self.last_messages = messages
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "test-model"


def _seed_health_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HealthWorkspace:
    monkeypatch.setenv("HEALTH_VAULT_KEY", "test-health-vault-key")
    health = HealthWorkspace(tmp_path)
    health.save_profile({"mode": "health", "preferred_channel": "telegram"})
    health.save_vault(
        {
            "identifiers": {
                "person_names": ["Ann"],
                "emails": ["ann@example.com"],
                "phones": ["+15551230000"],
                "chat_ids": ["12345"],
                "channels": ["telegram"],
            }
        }
    )
    return health


def test_tokenize_and_detokenize_round_trip_with_false_positive_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_health_workspace(tmp_path, monkeypatch)
    anonymizer = PIIAnonymizer(tmp_path)

    text = "Ann sent ann@example.com twice. Annex should stay literal."
    anonymized, token_map = anonymizer.tokenize(text)

    assert "[PERSON_001]" in anonymized
    assert "[EMAIL_001]" in anonymized
    assert "Annex" in anonymized
    assert anonymizer.detokenize(anonymized, token_map) == text


def test_anonymizer_handles_nested_tool_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_health_workspace(tmp_path, monkeypatch)
    anonymizer = PIIAnonymizer(tmp_path)
    messages = [
        {
            "role": "assistant",
            "content": "Calling a tool for Ann",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "record_note",
                        "arguments": {
                            "patient": "Ann",
                            "contact": {"email": "ann@example.com"},
                            "notes": ["Ann took the dose"],
                        },
                    },
                }
            ],
        }
    ]

    sanitized, token_map = anonymizer.anonymize_messages(messages)
    tool_args = sanitized[0]["tool_calls"][0]["function"]["arguments"]

    assert tool_args["patient"] == "[PERSON_001]"
    assert tool_args["contact"]["email"] == "[EMAIL_001]"

    response = LLMResponse(
        content="Send [PERSON_001] a reminder",
        tool_calls=[
            ToolCallRequest(
                id="call_2",
                name="send_message",
                arguments={"to": "[EMAIL_001]", "body": "Hi [PERSON_001]"},
            )
        ],
        reasoning_content="Reasoning about [PERSON_001]",
    )
    restored = anonymizer.deanonymize_response(response, token_map)

    assert restored.content == "Send Ann a reminder"
    assert restored.tool_calls[0].arguments["to"] == "ann@example.com"
    assert restored.tool_calls[0].arguments["body"] == "Hi Ann"
    assert restored.reasoning_content == "Reasoning about Ann"


@pytest.mark.asyncio
async def test_anonymizing_provider_preserves_retry_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_health_workspace(tmp_path, monkeypatch)
    inner = ScriptedProvider(
        [
            LLMResponse(content="429 rate limit", finish_reason="error"),
            LLMResponse(content="Hello [PERSON_001]"),
        ]
    )
    wrapped = AnonymizingProvider(inner, PIIAnonymizer(tmp_path))

    response = await wrapped.chat_with_retry(
        messages=[{"role": "user", "content": "Hi, I am Ann"}]
    )

    assert inner.calls == 2
    assert "[PERSON_001]" in inner.last_messages[0]["content"]
    assert response.content == "Hello Ann"
