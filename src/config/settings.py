from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: str  # not used by investigator.py — see azure_openai_v1_base_url
    azure_openai_deployment: str
    azure_openai_model: str = "gpt-4o"

    @property
    def azure_openai_v1_base_url(self) -> str:
        """Azure AI Foundry's unified v1 endpoint (native Responses API + Chat Completions).

        Normalises azure_openai_endpoint whether it's the bare resource root or already
        has /openai/v1 appended, so this is safe regardless of which form .env holds.
        """
        root = self.azure_openai_endpoint.rstrip("/")
        if root.endswith("/openai/v1"):
            return root
        return f"{root}/openai/v1"

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/nexus"
    redis_url: str = "redis://localhost:6379"

    max_concurrent_investigations: int = 10
    # Distributed semaphore (gateway/concurrency.py) — shared across every replica via Redis.
    # Lease comfortably above investigator.py's MAX_TURNS-bounded worst case so a healthy
    # long-running diagnosis is never pruned as if it were a crashed replica's stale slot.
    concurrency_lease_seconds: float = 20 * 60
    # How long a request queues (with backoff) for a slot before giving up — chosen because the
    # scenario that actually fills the cap (a real incident, many humans clicking Diagnose in the
    # same few minutes) is exactly when a hard rejection hurts most; queueing self-resolves as
    # slots free up instead.
    concurrency_max_wait_seconds: float = 120.0
    rerun_freshness_window_seconds: int = 7200

    nexus_base_url: str = "http://localhost:8000"

    # TEMPORARY dev/test shortcut (2026-07-24, explicit user call) — Key Vault isn't wired up
    # for testing yet. gateway/vault.py falls back to this for any project whose
    # project_factories row has no key_vault_uri set (no project-name matching needed — the
    # DB row itself is what marks a project as "no vault configured yet"). Every project with
    # a real key_vault_uri still goes through the real Key Vault path unchanged.
    # Remove once Key Vault is actually tested end-to-end.
    adf_client_secret: str | None = None

    # Outlook email delivery via Microsoft Graph (notifications/email.py) — reuses the SSO
    # Entra ID app registration (item 17) with an added Mail.Send permission. Optional:
    # notification delivery is a convenience layer, not a security gate, so missing config
    # degrades to "skip with a warning," unlike hmac_secret's fail-closed requirement.
    graph_tenant_id: str | None = None
    graph_client_id: str | None = None
    graph_client_secret: str | None = None
    notification_sender_upn: str | None = None

    # Required, no default — a missing/empty secret used to silently disable signature
    # verification (settings.hmac_secret is not None was True even for ""), which is a
    # fail-open bug: an empty-string HMAC key is trivially forgeable by anyone. Fail closed
    # instead: the app refuses to start without a real, non-empty secret configured.
    hmac_secret: str

    @field_validator("hmac_secret")
    @classmethod
    def _hmac_secret_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError(
                "HMAC_SECRET must be set to a real, non-empty value — "
                "an empty secret makes signature verification trivially forgeable."
            )
        return value

    # Rerun outcome polling
    rerun_outcome_check_interval_seconds: int = 300  # 5 min between checks
    rerun_outcome_max_checks: int = 3                # up to 15 min total

    # Denial escalation
    denial_threshold: int = 3  # override known-fix to full investigation after N denials

    # Batch detection
    batch_window_seconds: int = 300    # 5-minute sliding window
    batch_threshold: int = 20          # N failures on same platform → platform-outage batch alert


settings = Settings()
