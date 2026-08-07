from unittest.mock import patch

import pytest

from gateway.credential_resolution import CredentialResolutionError, resolve_client_secret


class _FakeRow:
    def __init__(self, client_secret):
        self._client_secret = client_secret

    def __getitem__(self, index):
        assert index == 0
        return self._client_secret


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeDb:
    def __init__(self, row):
        self._row = row
        self.executed_params = None

    async def execute(self, _stmt, params=None):
        self.executed_params = params
        return _FakeResult(self._row)


@pytest.fixture(autouse=True)
def _watchtower_key(monkeypatch):
    from gateway import credential_resolution
    monkeypatch.setattr(credential_resolution.settings, "watchtower_credential_key", "shared-passphrase")
    yield


@pytest.mark.asyncio
async def test_resolve_client_secret_decrypts_and_returns_it():
    db = _FakeDb(_FakeRow("ciphertext-blob"))
    with patch("gateway.credential_resolution.decrypt_cryptojs_aes", return_value="super-secret") as decrypt:
        secret = await resolve_client_secret(db, "acme")

    assert secret == "super-secret"
    assert db.executed_params == {"project": "acme"}
    decrypt.assert_called_once_with("ciphertext-blob", "shared-passphrase")


@pytest.mark.asyncio
async def test_resolve_client_secret_raises_when_no_credential_row():
    db = _FakeDb(None)
    with pytest.raises(CredentialResolutionError, match="has no ADF Credential row"):
        await resolve_client_secret(db, "acme")


@pytest.mark.asyncio
async def test_resolve_client_secret_raises_when_secret_column_is_empty():
    db = _FakeDb(_FakeRow(""))
    with pytest.raises(CredentialResolutionError, match="has no ADF Credential row"):
        await resolve_client_secret(db, "acme")


@pytest.mark.asyncio
async def test_resolve_client_secret_raises_when_watchtower_key_not_set(monkeypatch):
    from gateway import credential_resolution
    monkeypatch.setattr(credential_resolution.settings, "watchtower_credential_key", None)
    db = _FakeDb(_FakeRow("ciphertext-blob"))

    with pytest.raises(CredentialResolutionError, match="WATCHTOWER_CREDENTIAL_KEY is not set"):
        await resolve_client_secret(db, "acme")


@pytest.mark.asyncio
async def test_resolve_client_secret_wraps_decrypt_failure():
    db = _FakeDb(_FakeRow("corrupt-blob"))
    with patch("gateway.credential_resolution.decrypt_cryptojs_aes", side_effect=ValueError("bad padding")):
        with pytest.raises(CredentialResolutionError, match="Failed to decrypt client_secret"):
            await resolve_client_secret(db, "acme")
