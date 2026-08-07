-- Azure Key Vault is no longer used anywhere in this codebase (removed 2026-08-10,
-- gateway/vault.py deleted) — every project's client_secret is resolved directly from
-- WatchTower's own public."Credential".clientSecret instead (see
-- gateway/credential_resolution.py). This column has been dead since that change; dropping it
-- now that nothing reads or writes it.
ALTER TABLE "credentials" DROP COLUMN "key_vault_uri";
