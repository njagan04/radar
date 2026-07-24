import json
from unittest.mock import AsyncMock

import pytest

from gateway.rbac import RBACGateway


class _FakeScalars:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDb:
    def __init__(self, allowed: bool):
        self._allowed = allowed
        self.commit = AsyncMock()
        self.add = lambda *_a, **_k: None

    async def execute(self, _stmt):
        return _FakeScalars(self._allowed)


def _make_redis(client_secret="s3cr3t"):
    redis = AsyncMock()
    redis.get.return_value = json.dumps({"client_secret": client_secret})
    return redis


@pytest.mark.asyncio
async def test_dispatch_runs_sync_tool_via_executor(monkeypatch):
    def fake_sync_tool(**kwargs):
        return {"got": kwargs}

    monkeypatch.setattr("gateway.rbac._TOOL_REGISTRY", {"fake_sync_tool": fake_sync_tool})
    gateway = RBACGateway(db=_FakeDb(allowed=True), redis=_make_redis(), investigation_id="inv-1")

    result = await gateway.call(
        tool_name="fake_sync_tool", arguments={"x": 1},
        actor="system", pipeline_id="pl", project="acme", platform="adf",
    )

    assert result["got"]["x"] == 1
    assert result["got"]["client_secret"] == "s3cr3t"


@pytest.mark.asyncio
async def test_dispatch_awaits_async_tool_with_db_and_project_injected(monkeypatch):
    captured = {}

    async def fake_async_tool(db, project, **kwargs):
        captured["db"] = db
        captured["project"] = project
        captured["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setattr("gateway.rbac._TOOL_REGISTRY", {"fake_async_tool": fake_async_tool})
    fake_db = _FakeDb(allowed=True)
    gateway = RBACGateway(db=fake_db, redis=_make_redis(), investigation_id="inv-1")

    result = await gateway.call(
        tool_name="fake_async_tool", arguments={"y": 2},
        actor="system", pipeline_id="pl", project="acme", platform="adf",
    )

    assert result == {"ok": True}
    assert captured["db"] is fake_db
    assert captured["project"] == "acme"
    assert captured["kwargs"]["y"] == 2
    assert captured["kwargs"]["client_secret"] == "s3cr3t"


@pytest.mark.asyncio
async def test_dispatch_denies_disallowed_tool_before_calling_it():
    gateway = RBACGateway(db=_FakeDb(allowed=False), redis=_make_redis(), investigation_id="inv-1")

    with pytest.raises(PermissionError):
        await gateway.call(
            tool_name="anything", arguments={}, actor="system",
            pipeline_id="pl", project="acme", platform="adf",
        )
