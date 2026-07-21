"""
Per-project Key Vault credential resolution (Nexus's own Azure tenant, one vault per project).

Replaces the old flow where WatchTower forwarded a project's ADF client_secret in every
`pipeline-failure` event payload. Now the event only carries `project`; this module resolves
project -> ProjectMetadata.key_vault_uri -> the vault's "client-secret" entry -> the same
`creds:{investigation_id}` Redis cache the RBAC gateway already reads (6h TTL, unchanged).

Nexus's own service identity authenticates to its vaults via DefaultAzureCredential
(managed identity in Azure Container Apps, `az login` locally) — deliberately not a
client-secret-in-.env, since that would just move the "how do we bootstrap access to
secrets" problem instead of solving it.
"""
import json
import logging

import redis.asyncio as aioredis
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models import ProjectMetadata

logger = logging.getLogger(__name__)

_SECRET_NAME = "client-secret"
_CREDENTIAL = DefaultAzureCredential()


class VaultResolutionError(RuntimeError):
    """Raised when a project has no key_vault_uri configured, or the secret fetch fails."""


def _fetch_client_secret(key_vault_uri: str) -> str:
    client = SecretClient(vault_url=key_vault_uri, credential=_CREDENTIAL)
    return client.get_secret(_SECRET_NAME).value


async def populate_credentials(
    db_factory: async_sessionmaker,
    redis: aioredis.Redis,
    project: str,
    investigation_id: str,
    ttl_seconds: int = 6 * 3600,
) -> None:
    """
    Resolve `project`'s Key Vault, fetch its client_secret, and cache it in Redis under
    `creds:{investigation_id}` — the same key/shape/TTL `RBACGateway._enrich()` already reads,
    so nothing downstream of Redis needs to change.
    """
    async with db_factory() as db:
        result = await db.execute(
            select(ProjectMetadata.key_vault_uri).where(ProjectMetadata.project == project)
        )
        key_vault_uri = result.scalar_one_or_none()

    if not key_vault_uri:
        raise VaultResolutionError(
            f"project='{project}' has no key_vault_uri configured in project_metadata"
        )

    try:
        client_secret = _fetch_client_secret(key_vault_uri)
    except Exception as exc:
        raise VaultResolutionError(
            f"Failed to fetch client secret from vault '{key_vault_uri}' for project='{project}': {exc}"
        ) from exc

    await redis.set(
        f"creds:{investigation_id}",
        json.dumps({"client_secret": client_secret}),
        ex=ttl_seconds,
    )
