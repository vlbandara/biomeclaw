from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.health import worker as worker_mod


@pytest.mark.asyncio
async def test_run_builds_worker_with_explicit_settings(monkeypatch):
    captured = {}

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

        async def async_run(self):
            captured["ran"] = True

    monkeypatch.setattr(worker_mod, "Worker", FakeWorker)
    monkeypatch.setattr(
        worker_mod,
        "WorkerSettings",
        SimpleNamespace(
            functions=["spawn-instance-job"],
            redis_settings="redis-settings",
        ),
    )

    await worker_mod._run()

    assert captured["args"] == ()
    assert captured["kwargs"] == {
        "functions": ["spawn-instance-job"],
        "redis_settings": "redis-settings",
    }
    assert captured["ran"] is True
