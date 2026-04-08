from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient

from nanobot.health.api import create_app
from nanobot.health.storage import HealthWorkspace


class _ActiveTask:
    def done(self) -> bool:
        return False


def _make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, HealthWorkspace]:
    monkeypatch.delenv("NANOBOT_HEALTH_REGISTRY_URL", raising=False)
    monkeypatch.setenv("NANOBOT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HEALTH_VAULT_KEY", "test-health-vault-key")
    monkeypatch.setenv("HEALTH_TELEGRAM_BOT_URL", "https://t.me/example_bot")
    monkeypatch.setenv("HEALTH_WHATSAPP_CHAT_URL", "https://wa.me/15550001111")
    monkeypatch.setenv("NANOBOT_HEALTH_REGISTRY_PATH", str(tmp_path / "health-registry.sqlite3"))
    monkeypatch.setenv("APP_RELEASE", "test-release")
    monkeypatch.setenv("APP_DEPLOYED_AT", "2026-04-08T00:00:00Z")
    health = HealthWorkspace(tmp_path)
    app = create_app()
    return TestClient(app), health


def test_readyz_and_metrics_include_runtime_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _make_client(tmp_path, monkeypatch)
    client.app.state.instance_monitor_task = _ActiveTask()

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"
    assert ready.json()["checks"]["registry"]["status"] == "ok"

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    payload = metrics.json()
    assert payload["release"]["version"] == "test-release"
    assert payload["release"]["deployedAt"] == "2026-04-08T00:00:00Z"
    assert payload["readiness"]["status"] == "ok"
    assert payload["runtime"]["requests"]["success"] >= 1


def test_readyz_reports_degraded_registry_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _make_client(tmp_path, monkeypatch)
    client.app.state.instance_monitor_task = _ActiveTask()
    monkeypatch.setattr(
        client.app.state.registry,
        "list_users",
        AsyncMock(side_effect=RuntimeError("registry unavailable")),
    )

    ready = client.get("/readyz")

    assert ready.status_code == 503
    assert ready.json()["status"] == "degraded"
    assert ready.json()["checks"]["registry"]["status"] == "error"
    assert "registry unavailable" in ready.json()["checks"]["registry"]["detail"]


def test_signup_failure_returns_request_reference_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _ = _make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        client.app.state.registry,
        "create_user",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    caplog.set_level(logging.ERROR, logger="nanobot.health.api")

    response = client.post(
        "/api/signup",
        json={"name": "Vinod", "timezone": "Asia/Colombo"},
        headers={"X-Request-ID": "req-signup-123"},
    )

    assert response.status_code == 502
    assert response.headers["X-Request-ID"] == "req-signup-123"
    assert response.json() == {
        "detail": "Unable to create setup session right now.",
        "requestId": "req-signup-123",
        "errorId": "req-signup-123",
    }
    metrics = client.get("/metrics").json()
    assert metrics["runtime"]["lastError"]["requestId"] == "req-signup-123"
    assert metrics["runtime"]["lastError"]["statusCode"] == 502

    error_records = [record for record in caplog.records if record.msg == "signup.failed"]
    assert error_records
    assert getattr(error_records[0], "request_id", "") == "req-signup-123"
    assert getattr(error_records[0], "error_id", "") == "req-signup-123"


def test_validation_error_keeps_request_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _make_client(tmp_path, monkeypatch)

    response = client.post("/api/signup", json={}, headers={"X-Request-ID": "req-validation-1"})

    assert response.status_code == 422
    assert response.headers["X-Request-ID"] == "req-validation-1"
    assert response.json()["requestId"] == "req-validation-1"
    assert "errorId" not in response.json()
