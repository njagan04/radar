import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.vault import VaultResolutionError, populate_credentials


class _FakeScalars:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return _FakeScalars(self._value)


class _FakeDb:
    def __init__(self, key_vault_uri):
        self._key_vault_uri = key_vault_uri

    async def execute(self, _stmt):
        return _FakeScalarResult(self._key_vault_uri)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _db_factory(key_vault_uri):
    def factory():
        return _FakeDb(key_vault_uri)
    return factory


@pytest.mark.asyncio
async def test_populate_credentials_caches_secret_in_redis():
    redis = AsyncMock()
    with patch("gateway.vault._fetch_client_secret", return_value="super-secret") as fetch:
        await populate_credentials(
            db_factory=_db_factory("https://proj-vault.vault.azure.net/"),
            redis=redis,
            project="acme",
            investigation_id="inv-1",
            ttl_seconds=21600,
        )

    fetch.assert_called_once_with("https://proj-vault.vault.azure.net/")
    redis.set.assert_called_once()
    args, kwargs = redis.set.call_args
    assert args[0] == "creds:inv-1"
    assert json.loads(args[1]) == {"client_secret": "super-secret"}
    assert kwargs["ex"] == 21600


@pytest.mark.asyncio
async def test_populate_credentials_raises_when_no_vault_configured():
    redis = AsyncMock()
    # No key_vault_uri AND no ADF_CLIENT_SECRET fallback configured -> must still raise.
    with patch("gateway.vault.settings.adf_client_secret", None):
        with pytest.raises(VaultResolutionError, match="no key_vault_uri configured"):
            await populate_credentials(
                db_factory=_db_factory(None),
                redis=redis,
                project="acme",
                investigation_id="inv-1",
            )
    redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_populate_credentials_falls_back_to_adf_client_secret_when_no_vault():
    """
    Temporary dev/test shortcut (gateway/vault.py, 2026-07-24): a project with no
    key_vault_uri configured falls back to ADF_CLIENT_SECRET instead of failing, so the
    workflow can be tested before Key Vault is wired up. Remove this test alongside the
    fallback itself once Key Vault is tested end-to-end.
    """
    redis = AsyncMock()
    with patch("gateway.vault.settings.adf_client_secret", "fallback-secret"):
        await populate_credentials(
            db_factory=_db_factory(None),
            redis=redis,
            project="adf_mcp_test",
            investigation_id="inv-1",
            ttl_seconds=5400,
        )

    redis.set.assert_called_once()
    args, kwargs = redis.set.call_args
    assert args[0] == "creds:inv-1"
    assert json.loads(args[1]) == {"client_secret": "fallback-secret"}
    assert kwargs["ex"] == 5400


@pytest.mark.asyncio
async def test_populate_credentials_wraps_fetch_failure():
    redis = AsyncMock()
    with patch("gateway.vault._fetch_client_secret", side_effect=RuntimeError("vault unreachable")):
        with pytest.raises(VaultResolutionError, match="Failed to fetch client secret"):
            await populate_credentials(
                db_factory=_db_factory("https://proj-vault.vault.azure.net/"),
                redis=redis,
                project="acme",
                investigation_id="inv-1",
            )
    redis.set.assert_not_called()
