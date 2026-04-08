from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nanobot.health.api import create_app
from nanobot.health.storage import HealthWorkspace

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("starlette")


def _payload(full_name: str = "Jane Doe") -> dict:
    return {
        "phase1": {
            "full_name": full_name,
            "location": "Colombo, Sri Lanka",
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


def _minimal_payload(full_name: str = "Jane Doe") -> dict:
    return {
        "phase1": {
            "full_name": full_name,
            "location": "Colombo, Sri Lanka",
            "email": "",
            "phone": "",
            "timezone": "Asia/Colombo",
            "language": "en",
            "preferred_channel": "telegram",
            "age_range": "not set",
            "sex": "unknown",
            "gender": "not set",
            "height_cm": None,
            "weight_kg": None,
            "known_conditions": [],
            "medications": [],
            "allergies": [],
            "wake_time": "07:00",
            "sleep_time": "22:30",
            "consents": ["privacy", "emergency", "coaching"],
        },
        "phase2": {
            "mood_interest": 0,
            "mood_down": 0,
            "activity_level": "not set",
            "nutrition_quality": "not set",
            "sleep_quality": "not set",
            "stress_level": "not set",
            "goals": ["Protect sleep and recovery"],
            "current_concerns": "Evenings are when I drift.",
            "reminder_preferences": ["Warm, gentle nudges"],
            "medication_reminder_windows": [],
            "morning_check_in": True,
            "weekly_summary": True,
        },
    }


def _make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, HealthWorkspace]:
    monkeypatch.delenv("NANOBOT_HEALTH_REGISTRY_URL", raising=False)
    monkeypatch.setenv("NANOBOT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HEALTH_VAULT_KEY", "test-health-vault-key")
    monkeypatch.setenv("HEALTH_TELEGRAM_BOT_URL", "https://t.me/example_bot")
    monkeypatch.setenv("HEALTH_WHATSAPP_CHAT_URL", "https://wa.me/15550001111")
    health = HealthWorkspace(tmp_path)
    app = create_app()
    return TestClient(app), health


def test_onboard_get_and_submit_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, health = _make_client(tmp_path, monkeypatch)
    invite, _ = health.create_invite(channel="telegram", chat_id="123")

    page = client.get(f"/onboard/{invite}")
    assert page.status_code == 200
    assert "Let’s make this feel like your space." in page.text
    assert "Back to Telegram" in page.text

    resp = client.post(f"/api/onboard/{invite}/submit", json=_minimal_payload())
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["channelLinks"]["telegram"] == "https://t.me/example_bot"

    profile = json.loads((tmp_path / "health" / "profile.json").read_text(encoding="utf-8"))
    assert profile["preferred_channel"] == "telegram"
    assert profile["location"] == "Colombo, Sri Lanka"
    assert profile["goals"] == ["Protect sleep and recovery"]
    assert profile["demographics"]["known_conditions"] == []
    vault_ciphertext = (tmp_path / "health" / "vault.json.enc").read_text(encoding="utf-8")
    assert "Jane Doe" not in vault_ciphertext

    used_page = client.get(f"/onboard/{invite}")
    assert used_page.status_code == 404


def test_onboard_rejects_expired_invite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, health = _make_client(tmp_path, monkeypatch)
    invite, meta = health.create_invite(channel="telegram", chat_id="123")
    invites = health.load_invites()
    meta["expires_at"] = "2000-01-01T00:00:00+00:00"
    invites[invite] = meta
    health.save_invites(invites)

    resp = client.get(f"/onboard/{invite}")
    assert resp.status_code == 404


def test_onboard_regeneration_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, health = _make_client(tmp_path, monkeypatch)
    first_invite, _ = health.create_invite(channel="telegram", chat_id="123")
    assert client.post(f"/api/onboard/{first_invite}/submit", json=_payload()).status_code == 200

    second_invite, _ = health.create_invite(channel="telegram", chat_id="123")
    payload = _payload("Jane R. Doe")
    payload["phase2"]["goals"] = ["improve sleep", "walk daily"]
    resp = client.post(f"/api/onboard/{second_invite}/submit", json=payload)

    assert resp.status_code == 200
    profile = json.loads((tmp_path / "health" / "profile.json").read_text(encoding="utf-8"))
    assert profile["user_token"] == "USER-001"
    assert profile["goals"] == ["improve sleep", "walk daily"]


def test_onboard_accepts_reduced_story_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, health = _make_client(tmp_path, monkeypatch)
    invite, _ = health.create_invite(channel="telegram", chat_id="123")

    resp = client.post(f"/api/onboard/{invite}/submit", json=_minimal_payload("Sam Rivera"))

    assert resp.status_code == 200
    profile = json.loads((tmp_path / "health" / "profile.json").read_text(encoding="utf-8"))
    assert profile["timezone"] == "Asia/Colombo"
    assert profile["wellbeing"]["activity_level"] == "not set"
    assert profile["preferences"]["reminder_preferences"] == ["Warm, gentle nudges"]
    vault_ciphertext = (tmp_path / "health" / "vault.json.enc").read_text(encoding="utf-8")
    assert "Sam Rivera" not in vault_ciphertext
