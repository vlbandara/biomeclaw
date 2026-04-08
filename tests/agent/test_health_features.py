from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import Dream, MemoryStore
from nanobot.agent.tools.health_profile import SetPreferredNameTool
from nanobot.bus.queue import MessageBus
from nanobot.cli.commands import _pick_routable_heartbeat_target
from nanobot.health.bootstrap import persist_health_onboarding, write_health_workspace_assets
from nanobot.health.storage import HealthWorkspace
from nanobot.session.manager import SessionManager


def _health_profile(preferred_channel: str = "whatsapp") -> dict:
    return {
        "mode": "health",
        "preferred_channel": preferred_channel,
        "timezone": "UTC",
        "language": "en",
        "demographics": {},
        "routines": {},
        "screenings": {},
        "wellbeing": {},
        "goals": ["stay consistent"],
        "current_concerns": "",
        "preferences": {
            "morning_check_in": True,
            "reminder_preferences": [],
            "medication_reminder_windows": [],
            "weekly_summary": True,
        },
    }


def _enable_health(workspace: Path) -> None:
    health = HealthWorkspace(workspace)
    profile = _health_profile()
    health.save_profile(profile)
    write_health_workspace_assets(workspace, profile)


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


@pytest.mark.asyncio
async def test_health_cold_start_injects_opening_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_health(tmp_path)
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096

    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    captured: list[list[dict]] = []

    async def fake_run(initial_messages, *args, **kwargs):
        captured.append(initial_messages)
        return "fine", [], initial_messages + [{"role": "assistant", "content": "fine"}]

    monkeypatch.setattr(loop, "_run_agent_loop", fake_run)

    await loop.process_direct("hey", channel="telegram", chat_id="123")

    system_prompt = captured[0][0]["content"]
    assert "Health Cold Start" in system_prompt
    assert "feel alive or generic" in system_prompt


@pytest.mark.asyncio
async def test_health_cold_start_turns_off_after_substantive_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_health(tmp_path)
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096

    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    captured: list[list[dict]] = []

    async def fake_run(initial_messages, *args, **kwargs):
        captured.append(initial_messages)
        return "fine", [], initial_messages + [{"role": "assistant", "content": "fine"}]

    monkeypatch.setattr(loop, "_run_agent_loop", fake_run)

    await loop.process_direct(
        "I slept badly, skipped the gym, and I need a reset.",
        session_key="telegram:123",
        channel="telegram",
        chat_id="123",
    )
    await loop.process_direct(
        "What now?",
        session_key="telegram:123",
        channel="telegram",
        chat_id="123",
    )

    assert "Health Cold Start" in captured[0][0]["content"]
    assert "Health Cold Start" not in captured[1][0]["content"]


@pytest.mark.asyncio
async def test_onboarding_seeds_preferred_name_and_runtime_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEALTH_VAULT_KEY", "test-health-vault-key")
    persist_health_onboarding(
        tmp_path,
        {
            "phase1": {
                "full_name": "Jane Doe",
                "email": "",
                "phone": "",
                "timezone": "UTC",
                "language": "en",
                "preferred_channel": "telegram",
                "age_range": "not set",
                "sex": "not set",
                "gender": "not set",
                "height_cm": None,
                "weight_kg": None,
                "known_conditions": [],
                "medications": [],
                "allergies": [],
                "wake_time": "",
                "sleep_time": "",
                "consents": ["privacy"],
            },
            "phase2": {
                "mood_interest": 0,
                "mood_down": 0,
                "activity_level": "not set",
                "nutrition_quality": "not set",
                "sleep_quality": "not set",
                "stress_level": "not set",
                "goals": ["Gym consistency"],
                "current_concerns": "",
                "reminder_preferences": [],
                "medication_reminder_windows": [],
                "morning_check_in": True,
                "weekly_summary": True,
            },
        },
        invite={"channel": "telegram", "chat_id": "123"},
        secret="test-health-vault-key",
    )

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096

    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    captured: list[list[dict]] = []

    async def fake_run(initial_messages, *args, **kwargs):
        captured.append(initial_messages)
        return "fine", [], initial_messages + [{"role": "assistant", "content": "fine"}]

    monkeypatch.setattr(loop, "_run_agent_loop", fake_run)

    await loop.process_direct("hey", session_key="telegram:123", channel="telegram", chat_id="123")

    health = HealthWorkspace(tmp_path)
    assert health.load_preferred_name(secret="test-health-vault-key") == "Jane"
    assert "Jane Doe" not in (tmp_path / "USER.md").read_text(encoding="utf-8")
    assert "Jane" not in (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "Preferred Name: Jane" in captured[0][-1]["content"]


@pytest.mark.asyncio
async def test_set_preferred_name_tool_persists_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEALTH_VAULT_KEY", "test-health-vault-key")
    _enable_health(tmp_path)
    health = HealthWorkspace(tmp_path)
    health.save_vault(
        {
            "identifiers": {"person_names": []},
            "contact": {"full_name": ""},
        },
        secret="test-health-vault-key",
    )

    tool = SetPreferredNameTool(tmp_path)
    result = await tool.execute(preferred_name="Vin")

    vault = health.load_vault(secret="test-health-vault-key")
    assert result == "Saved preferred name: Vin"
    assert health.load_preferred_name(secret="test-health-vault-key") == "Vin"
    assert "Vin" in vault["identifiers"]["person_names"]
