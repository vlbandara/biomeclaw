from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nanobot.health.registry import HealthRegistry


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def execute(self, *args: object) -> None:
        self.calls.append(args)


class _FakeAcquire:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakePool:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.connection)


@pytest.mark.asyncio
async def test_postgres_create_user_uses_expected_insert_order() -> None:
    connection = _FakeConnection()
    registry = HealthRegistry()
    registry.url = "postgresql://example.invalid/db"
    registry._pool = _FakePool(connection)  # type: ignore[assignment]
    registry.init = AsyncMock(return_value=None)  # type: ignore[method-assign]

    record = await registry.create_user(name="Vinod", timezone="Asia/Colombo")

    assert record.status == "setup"
    assert len(connection.calls) == 1
    _, user_id, name, timezone, setup_token, tier, status, created_at = connection.calls[0]
    assert user_id == record.id
    assert name == "Vinod"
    assert timezone == "Asia/Colombo"
    assert setup_token == record.setup_token
    assert tier == "standard"
    assert status == "setup"
    assert created_at == record.created_at
