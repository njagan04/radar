from unittest.mock import AsyncMock, MagicMock

import pytest
from azure.core.exceptions import ClientAuthenticationError

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


def _patch_resolve(monkeypatch, client_secret="s3cr3t"):
    monkeypatch.setattr(
        "gateway.rbac.resolve_client_secret", AsyncMock(return_value=client_secret)
    )


@pytest.mark.asyncio
async def test_dispatch_runs_sync_tool_via_executor(monkeypatch):
    def fake_sync_tool(**kwargs):
        return {"got": kwargs}

    monkeypatch.setattr(
        "gateway.rbac._TOOL_REGISTRY", {"fake_sync_tool": fake_sync_tool}
    )
    _patch_resolve(monkeypatch)
    gateway = RBACGateway(db=_FakeDb(allowed=True))

    result = await gateway.call(
        tool_name="fake_sync_tool",
        arguments={"x": 1},
        user_id="system",
        pipeline_id="pl",
        project="acme",
        platform="adf",
    )

    assert result["got"]["x"] == 1
    assert result["got"]["client_secret"] == "s3cr3t"


@pytest.mark.asyncio
async def test_dispatch_denies_disallowed_tool_before_calling_it(monkeypatch):
    _patch_resolve(monkeypatch)
    gateway = RBACGateway(db=_FakeDb(allowed=False))

    with pytest.raises(PermissionError):
        await gateway.call(
            tool_name="anything",
            arguments={},
            user_id="system",
            pipeline_id="pl",
            project="acme",
            platform="adf",
        )


@pytest.mark.asyncio
async def test_client_authentication_error_invalidates_cache_entry_and_still_propagates(
    monkeypatch,
):
    """A stale/rotated credential surfaces as ClientAuthenticationError from the Azure SDK call
    inside a tool function. _dispatch() must evict that project's cached client (so the NEXT
    call rebuilds fresh) without swallowing the error — this call still fails, only the next
    one gets a chance to succeed."""

    def failing_tool(**kwargs):
        raise ClientAuthenticationError(message="invalid_client secret")

    monkeypatch.setattr("gateway.rbac._TOOL_REGISTRY", {"failing_tool": failing_tool})
    _patch_resolve(monkeypatch, client_secret="rotated-secret")
    invalidate = MagicMock()
    monkeypatch.setattr("gateway.rbac.client_cache.invalidate", invalidate)
    gateway = RBACGateway(
        db=_FakeDb(allowed=True),
        infra_params={"tenant_id": "t1", "client_id": "c1", "subscription_id": "s1"},
    )

    with pytest.raises(ClientAuthenticationError):
        await gateway.call(
            tool_name="failing_tool",
            arguments={},
            user_id="system",
            pipeline_id="pl",
            project="acme",
            platform="adf",
        )

    invalidate.assert_called_once_with("t1", "c1", "rotated-secret", "s1")


@pytest.mark.asyncio
async def test_enrich_arguments_cannot_override_trusted_infra_params(monkeypatch):
    """A model can't be schema-restricted from emitting extra keys (FunctionTool is built with
    strict_json_schema=False), so if it ever emits e.g. "tenant_id" as an extra argument,
    _enrich() must not let it override the investigation's real project identity — that would
    let a chat turn for Project A silently execute the Azure call against Project B's tenant."""
    captured = {}

    def fake_sync_tool(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(
        "gateway.rbac._TOOL_REGISTRY", {"fake_sync_tool": fake_sync_tool}
    )
    _patch_resolve(monkeypatch, client_secret="real-secret")
    gateway = RBACGateway(
        db=_FakeDb(allowed=True),
        infra_params={"tenant_id": "trusted-tenant", "subscription_id": "trusted-sub"},
    )

    await gateway.call(
        tool_name="fake_sync_tool",
        arguments={"tenant_id": "attacker-tenant", "pipeline_name": "CustomerLoad"},
        user_id="system",
        pipeline_id="pl",
        project="acme",
        platform="adf",
    )

    assert captured["tenant_id"] == "trusted-tenant"
    assert captured["subscription_id"] == "trusted-sub"
    assert captured["pipeline_name"] == "CustomerLoad"
    assert captured["client_secret"] == "real-secret"


@pytest.mark.asyncio
async def test_enrich_resolves_client_secret_for_the_calling_project(monkeypatch):
    """resolve_client_secret must be asked about the tool call's actual project, not some
    other value — this is the trust boundary the whole cache design depends on."""
    resolve = AsyncMock(return_value="s3cr3t")
    monkeypatch.setattr("gateway.rbac.resolve_client_secret", resolve)
    monkeypatch.setattr(
        "gateway.rbac._TOOL_REGISTRY", {"fake_sync_tool": lambda **kwargs: {"ok": True}}
    )
    gateway = RBACGateway(db=_FakeDb(allowed=True))

    await gateway.call(
        tool_name="fake_sync_tool",
        arguments={},
        user_id="system",
        pipeline_id="pl",
        project="acme",
        platform="adf",
    )

    resolve.assert_called_once()
    args, _kwargs = resolve.call_args
    assert args[1] == "acme"


@pytest.mark.asyncio
async def test_log_includes_arguments_but_never_client_secret(monkeypatch):
    """RBACGateway._log()'s detail must include the caller-supplied arguments (so the audit
    trail can tell which resource/kind a distinct-named chat tool touched — see the "Expose
    all ADF tools" plan's audit-granularity fix), but never the enriched client_secret, since
    _log() is called before _enrich() runs."""
    added = []

    def fake_sync_tool(**kwargs):
        return {"ok": True}

    monkeypatch.setattr(
        "gateway.rbac._TOOL_REGISTRY", {"update_resource_definition": fake_sync_tool}
    )
    _patch_resolve(monkeypatch)

    fake_db = _FakeDb(allowed=True)
    fake_db.add = added.append
    gateway = RBACGateway(db=fake_db)

    await gateway.call(
        tool_name="update_resource_definition",
        arguments={
            "resource_type": "dataset",
            "name": "Foo",
            "reason": "test",
            "definition": {"type": "AzureSqlTable"},
        },
        user_id="alice@acme.com",
        pipeline_id="pl",
        project="acme",
        platform="adf",
    )

    assert len(added) == 1
    entry = added[0]
    assert entry.detail["tool"] == "update_resource_definition"
    assert entry.detail["arguments"] == {
        "resource_type": "dataset",
        "name": "Foo",
        "reason": "test",
        "definition": {"type": "AzureSqlTable"},
    }
    assert "client_secret" not in entry.detail["arguments"]
    assert entry.user_id == "alice@acme.com"
