from azure.identity import ClientSecretCredential


def get_credential(
    tenant_id: str, client_id: str, client_secret: str
) -> ClientSecretCredential:
    # Credentials are per-project (Option B — customer-provisioned SP).
    # Caller (RBAC gateway _enrich) fetches client_secret from Key Vault before calling this.
    return ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )
