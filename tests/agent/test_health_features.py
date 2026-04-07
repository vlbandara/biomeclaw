from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import Dream, MemoryStore
from nanobot.bus.queue import MessageBus
from nanobot.cli.commands import _pick_routable_heartbeat_target
from nanobot.health.storage import HealthWorkspace
from nanobot.session.manager import SessionManager


def _enable_health(workspace: Path) -> None:
    health = HealthWorkspace(workspace)
    health.save_profile({"mode": "health", "preferred_channel": "whatsapp"})


@pytest.mark.asyncio
async def test_health_emergency_language_short_circuits_agent(tmp_path: Path) -> None:
    _enable_health(tmp_path)
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096
    provider.chat_with_retry = AsyncMock()

    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    response = await loop.process_direct(
        "I have severe chest pain and can't breathe",
        channel="whatsapp",
        chat_id="123@s.whatsapp.net",
    )

    assert "local emergency services" in response.content
    provider.chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_dream_injects_health_appendix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_health(tmp_path)
    store = MemoryStore(tmp_path)
    store.append_history("User reports better sleep this week.")

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=SimpleNamespace(content="analysis"))
    provider.generation.max_tokens = 4096

    dream = Dream(store=store, provider=provider, model="test-model")

    async def fake_run(*args, **kwargs):
        return SimpleNamespace(stop_reason="completed", tool_events=[])

    monkeypatch.setattr(dream._runner, "run", fake_run)

    assert await dream.run() is True
    system_prompt = provider.chat_with_retry.await_args.kwargs["messages"][0]["content"]
    assert "Health Dream Appendix" in system_prompt


def test_preferred_channel_is_selected_for_heartbeat(tmp_path: Path) -> None:
    health = HealthWorkspace(tmp_path)
    health.save_profile({"mode": "health", "preferred_channel": "whatsapp"})

    manager = SessionManager(tmp_path)
    session = manager.get_or_create("whatsapp:123@s.whatsapp.net")
    manager.save(session)
    telegram = manager.get_or_create("telegram:999")
    manager.save(telegram)

    channel, chat_id = _pick_routable_heartbeat_target(
        workspace=tmp_path,
        enabled_channels={"telegram", "whatsapp"},
        session_manager=manager,
    )

    assert channel == "whatsapp"
    assert chat_id == "123@s.whatsapp.net"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,chat_id", [("telegram", "123"), ("whatsapp", "123@s.whatsapp.net")])
async def test_onboard_command_creates_invite_for_supported_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    chat_id: str,
) -> None:
    monkeypatch.setenv("HEALTH_ONBOARDING_BASE_URL", "https://health.example.com")
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096

    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    response = await loop.process_direct("/onboard", channel=channel, chat_id=chat_id)

    invites = json.loads((tmp_path / "health" / "invites.json").read_text(encoding="utf-8"))
    assert len(invites) == 1
    invite = next(iter(invites.values()))
    assert invite["channel"] == channel
    assert invite["chat_id"] == chat_id
    assert "https://health.example.com/onboard/" in response.content


@pytest.mark.asyncio
async def test_first_health_message_redirects_to_onboarding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOBOT_HEALTH_MODE", "1")
    monkeypatch.setenv("HEALTH_ONBOARDING_BASE_URL", "https://health.example.com")

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096
    provider.chat_with_retry = AsyncMock()

    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    response = await loop.process_direct("hello", channel="telegram", chat_id="123")

    assert "finish the health onboarding form" in response.content
    assert "https://health.example.com/onboard/" in response.content
    provider.chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_health_message_prefers_hosted_setup_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NANOBOT_HEALTH_MODE", "1")
    monkeypatch.setenv("HEALTH_ONBOARDING_BASE_URL", "https://health.example.com")

    HealthWorkspace(tmp_path).create_setup_session()

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096
    provider.chat_with_retry = AsyncMock()

    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    response = await loop.process_direct("hello", channel="telegram", chat_id="123")

    assert "finish setting up your health assistant" in response.content
    assert "https://health.example.com/setup/" in response.content
    provider.chat_with_retry.assert_not_awaited()
