from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient

from nanobot.health.api import create_app
from nanobot.health.storage import HealthWorkspace


def _payload() -> dict:
    return {
        "phase1": {
            "full_name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "+15550001111",
            "timezone": "Asia/Colombo",
            "language": "en",
            "preferred_channel": "telegram",
            "age_range": "35-44",
            "sex": "female",
            "gender": "woman",
            "height_cm": 165,
            "weight_kg": 62,
            "known_conditions": ["asthma"],
            "medications": ["albuterol"],
            "allergies": ["penicillin"],
            "wake_time": "06:30",
            "sleep_time": "22:30",
            "consents": ["privacy", "emergency", "coaching"],
        },
        "phase2": {
            "mood_interest": 1,
            "mood_down": 0,
            "activity_level": "moderate",
            "nutrition_quality": "mixed",
            "sleep_quality": "fair",
            "stress_level": "moderate",
            "goals": ["improve sleep"],
            "current_concerns": "Waking up tired",
            "reminder_preferences": ["morning check-in"],
            "medication_reminder_windows": ["08:00", "20:00"],
            "morning_check_in": True,
            "weekly_summary": True,
        },
    }


def _make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, HealthWorkspace]:
    monkeypatch.setenv("NANOBOT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HEALTH_VAULT_KEY", "test-health-vault-key")
    monkeypatch.setenv("HEALTH_TELEGRAM_BOT_URL", "https://t.me/example_bot")
    monkeypatch.setenv("HEALTH_WHATSAPP_CHAT_URL", "https://wa.me/15550001111")
    health = HealthWorkspace(tmp_path)
    app = create_app()
    return TestClient(app), health


def test_setup_page_and_provider_submission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, health = _make_client(tmp_path, monkeypatch)
    token, _ = health.create_setup_session()
    monkeypatch.setattr(
        "nanobot.health.api.validate_provider_credentials",
        AsyncMock(return_value={"provider": "minimax", "label": "MiniMax", "model": "MiniMax-M2.7"}),
    )

    page = client.get(f"/setup/{token}")
    assert page.status_code == 200
    assert "Set up your assistant" in page.text

    resp = client.post(
        f"/api/setup/{token}/provider",
        json={"provider": "minimax", "api_key": "minimax-secret-key"},
    )
    assert resp.status_code == 200
    setup = health.load_setup()
    assert setup["provider"]["validated_at"]
    assert setup["provider"]["provider"] == "minimax"
    ciphertext = (tmp_path / "health" / "setup-secrets.json.enc").read_text(encoding="utf-8")
    assert "minimax-secret-key" not in ciphertext


def test_setup_activate_with_telegram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, health = _make_client(tmp_path, monkeypatch)
    token, _ = health.create_setup_session()
    monkeypatch.setattr(
        "nanobot.health.api.validate_provider_credentials",
        AsyncMock(return_value={"provider": "minimax", "label": "MiniMax", "model": "MiniMax-M2.7"}),
    )
    monkeypatch.setattr(
        "nanobot.health.api.validate_telegram_bot_token",
        AsyncMock(
            return_value={
                "bot_id": 123456,
                "bot_username": "healthbot_test",
                "bot_name": "Health Bot",
                "bot_url": "https://t.me/healthbot_test",
            }
        ),
    )
    register = AsyncMock()
    monkeypatch.setattr("nanobot.health.api.register_telegram_commands", register)

    assert client.post(
        f"/api/setup/{token}/provider",
        json={"provider": "minimax", "api_key": "minimax-secret-key"},
    ).status_code == 200
    assert client.post(
        f"/api/setup/{token}/channels/telegram",
        json={"bot_token": "123:abc"},
    ).status_code == 200
    assert client.post(f"/api/setup/{token}/profile", json=_payload()).status_code == 200

    resp = client.post(f"/api/setup/{token}/activate")
    assert resp.status_code == 200
    assert resp.json()["state"] == "active"
    assert resp.json()["channelLinks"]["telegram"] == "https://t.me/healthbot_test"
    profile = json.loads((tmp_path / "health" / "profile.json").read_text(encoding="utf-8"))
    assert profile["preferred_channel"] == "telegram"
    assert health.load_setup()["state"] == "active"
    register.assert_awaited_once()


def test_setup_rejects_activation_without_connected_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, health = _make_client(tmp_path, monkeypatch)
    token, _ = health.create_setup_session()
    monkeypatch.setattr(
        "nanobot.health.api.validate_provider_credentials",
        AsyncMock(return_value={"provider": "minimax", "label": "MiniMax", "model": "MiniMax-M2.7"}),
    )

    assert client.post(
        f"/api/setup/{token}/provider",
        json={"provider": "minimax", "api_key": "minimax-secret-key"},
    ).status_code == 200
    assert client.post(f"/api/setup/{token}/profile", json=_payload()).status_code == 200

    resp = client.post(f"/api/setup/{token}/activate")
    assert resp.status_code == 400
    assert "Connect Telegram or WhatsApp first." in resp.json()["detail"]


def test_setup_whatsapp_status_uses_bridge_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, health = _make_client(tmp_path, monkeypatch)
    token, _ = health.create_setup_session()
    client.app.state.whatsapp_monitor._snapshot.update(
        {
            "status": "connected",
            "qr": "",
            "jid": "15550001111@s.whatsapp.net",
            "phone": "15550001111",
            "chat_url": "https://wa.me/15550001111",
        }
    )

    resp = client.get(f"/api/setup/{token}/channels/whatsapp/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "connected"
    setup = health.load_setup()
    assert setup["channels"]["whatsapp"]["connected"] is True


def test_setup_allows_openrouter_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, health = _make_client(tmp_path, monkeypatch)
    token, _ = health.create_setup_session()
    monkeypatch.setattr(
        "nanobot.health.api.validate_provider_credentials",
        AsyncMock(
            return_value={
                "provider": "openrouter",
                "label": "OpenRouter",
                "model": "openai/gpt-4o-mini",
            }
        ),
    )

    resp = client.post(
        f"/api/setup/{token}/provider",
        json={"provider": "openrouter", "api_key": "sk-or-test"},
    )

    assert resp.status_code == 200
    setup = health.load_setup()
    assert setup["provider"]["provider"] == "openrouter"
    assert setup["provider"]["model"] == "openai/gpt-4o-mini"
