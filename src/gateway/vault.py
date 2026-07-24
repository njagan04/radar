"""
Per-factory Key Vault credential resolution (Nexus's own Azure tenant).

Replaces the old flow where WatchTower forwarded a project's ADF client_secret in every
`pipeline-failure` event payload. Now the event only carries `project`; this module resolves
project -> ProjectFactory.key_vault_uri -> the vault's "client-secret" entry -> Redis, cached
under `creds:{investigation_id}` with a short TTL (1-2h) and refreshed transparently on lapse
or thread idle — see gateway/rbac.py's RBACGateway._enrich() for the read side.

Only client_secret lives in the vault — tenant_id/client_id/subscription_id are plain
ProjectFactory columns (identifiers, not credentials; see db/models.py).

Nexus's own service identity authenticates to its vaults via DefaultAzureCredential
(managed identity in Azure Container Apps, `az login` locally) — deliberately not a
client-secret-in-.env for the real path, since that would just move the "how do we
bootstrap access to secrets" problem instead of solving it. The one exception is the
explicit, temporary ADF_CLIENT_SECRET testing fallback below, used only for projects with
no key_vault_uri configured yet — not the production credential path.
"""
import json
import logging

import redis.asyncio as aioredis
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from config.settings import settings
from db.models import ProjectFactory

logger = logging.getLogger(__name__)

_SECRET_NAME = "client-secret"
_CREDENTIAL = DefaultAzureCredential()

# 1-2h — deliberately shorter than the original 6h figure; tune up later if it proves too
# aggressive. Not yet wired to idle-eviction (evicting the cache early on thread inactivity,
# independent of this TTL) — that's a separate, not-yet-built piece.
_DEFAULT_TTL_SECONDS = 90 * 60


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
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> None:
    """
    Resolve `project`'s Key Vault, fetch its client_secret, and cache it in Redis under
    `creds:{investigation_id}` — the same key/shape `RBACGateway._enrich()` already reads,
    so nothing downstream of Redis needs to change.

    Current scope (2026-07-24): a project has exactly one ProjectFactory row.
    """
    async with db_factory() as db:
        result = await db.execute(
            select(ProjectFactory.key_vault_uri).where(ProjectFactory.project == project)
        )
        key_vault_uri = result.scalars().first()

    if not key_vault_uri:
        if not settings.adf_client_secret:
            raise VaultResolutionError(
                f"project='{project}' has no key_vault_uri configured in project_factories, "
                "and no ADF_CLIENT_SECRET fallback is set in .env"
            )
        logger.warning(
            "project='%s' has no key_vault_uri — using ADF_CLIENT_SECRET from .env instead "
            "of a real Key Vault fetch. Temporary testing shortcut, not for production use.",
            project,
        )
        client_secret = settings.adf_client_secret
    else:
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
