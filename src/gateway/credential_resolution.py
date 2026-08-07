"""
Per-project ADF client_secret resolution — reads WatchTower's own encrypted
public."Credential" table directly, on every RBACGateway tool call.

Replaces the old two-stage flow (gateway/vault.py: Key Vault fetch, populated once per
investigation/thread into a 90-minute Redis cache that RBACGateway._enrich() read back).
Key Vault is no longer used at all: public."Credential".clientSecret — already stored there
for WatchTower's own Integrations feature — is the one and only source. Decryption is a local
AES operation (no network I/O), and RBACGateway already holds an open DB session for the same
call (permission check + audit log), so there's no network round trip left to amortize with a
cache — the secret is resolved fresh, per call, straight from that session.

tenant_id/client_id/subscription_id/resource_group/factory_name are NOT resolved here — they're
plain (non-secret) columns on RADAR's own `credentials` table, sourced via
InvestigationState/RBACGateway.infra_params() instead. Only client_secret lives behind this
decrypt.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from gateway.watchtower_crypto import decrypt_cryptojs_aes


class CredentialResolutionError(RuntimeError):
    """Raised when a project has no ADF Credential row, or its secret fails to decrypt."""


async def resolve_client_secret(db: AsyncSession, project: str) -> str:
    """Cross-schema read of WatchTower's own Integrations credential store — same pattern as
    chat/access.py's require_project_access. Matched on projectName + service name='adf' since
    a project could in principle have Credential rows for other services too. Takes the
    caller's already-open AsyncSession directly (not a factory) — this is a plain read
    alongside whatever else that session is already doing in the same request."""
    if not settings.watchtower_credential_key:
        raise CredentialResolutionError(
            f"project='{project}': WATCHTOWER_CREDENTIAL_KEY is not set in .env — cannot "
            "decrypt the ADF client_secret"
        )

    result = await db.execute(
        text(
            'SELECT c."clientSecret" FROM public."Credential" c '
            'JOIN public."Service" s ON s.id = c."serviceId" '
            'WHERE c."projectName" = :project AND s.name = \'adf\''
        ),
        {"project": project},
    )
    row = result.first()
    if row is None or not row[0]:
        raise CredentialResolutionError(
            f"project='{project}' has no ADF Credential row in WatchTower's public.\"Credential\" table"
        )
    try:
        return decrypt_cryptojs_aes(row[0], settings.watchtower_credential_key)
    except Exception as exc:
        raise CredentialResolutionError(
            f"Failed to decrypt client_secret for project='{project}': {exc}"
        ) from exc
