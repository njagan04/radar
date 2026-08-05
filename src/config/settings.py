from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: str  # not used by the chat agent — see azure_openai_v1_base_url
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

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/watchtower"
    # Which Postgres schema inside that database holds RADAR's tables (main.py's engine sets
    # this as the connection's search_path via connect_args — see its comment for why a plain
    # "?options=" query param on the URL doesn't work with SQLAlchemy's asyncpg dialect).
    radar_db_schema: str = "radar"
    redis_url: str = "redis://localhost:6379"

    max_concurrent_investigations: int = 10
    # Distributed semaphore (gateway/concurrency.py) — shared across every replica via Redis.
    # Lease comfortably above llm/agent.py's MAX_TURNS-bounded worst case so a healthy
    # long-running diagnosis is never pruned as if it were a crashed replica's stale slot.
    concurrency_lease_seconds: float = 20 * 60
    # How long a request queues (with backoff) for a slot before giving up — chosen because the
    # scenario that actually fills the cap (a real incident, many humans clicking Diagnose in the
    # same few minutes) is exactly when a hard rejection hurts most; queueing self-resolves as
    # slots free up instead.
    concurrency_max_wait_seconds: float = 120.0

    radar_base_url: str = "http://localhost:8000"

    # Replaces the old single, global ADF_CLIENT_SECRET fallback (2026-08-04) — that had a real
    # bug: every project without a vault configured silently shared the exact one secret.
    # gateway/credential_resolution.py reads every project's client_secret straight from
    # WatchTower's own public."Credential".clientSecret (already stored there for the
    # Integrations feature — no need for RADAR to keep its own second copy), decrypted with
    # this same passphrase WatchTower's lib/crypto.ts::decryptData uses (must match its
    # JWT_SECRET_KEY exactly). Azure Key Vault is no longer used for this at all (removed
    # 2026-08-10).
    watchtower_credential_key: str | None = None

    # Gates FastAPI's interactive docs (main.py) — "production" disables /docs, /redoc, and the
    # raw /openapi.json; anything else (the default) leaves them on for local/dev use.
    environment: str = "development"

    # Verifies the X-Radar-Assertion header WatchTower's proxy sends on every chat call
    # (chat/deps.py) — a short-lived HS256 JWT proving a real, SSO-verified WatchTower user is
    # behind the request, minted by WatchTower's own backend with this same shared secret.
    # Required for the same reason hmac_secret is: an unset/empty secret must not silently
    # disable verification.
    radar_assertion_secret: str

    # Required, no default — a missing/empty secret used to silently disable signature
    # verification (settings.hmac_secret is not None was True even for ""), which is a
    # fail-open bug: an empty-string HMAC key is trivially forgeable by anyone. Fail closed
    # instead: the app refuses to start without a real, non-empty secret configured. Same
    # reasoning applies to radar_assertion_secret — one shared validator for both instead of
    # two near-identical hand-rolled copies.
    hmac_secret: str

    @field_validator("radar_assertion_secret", "hmac_secret")
    @classmethod
    def _secret_not_empty(cls, value: str, info) -> str:
        if not value or not value.strip():
            raise ValueError(
                f"{info.field_name.upper()} must be set to a real, non-empty value — "
                "an empty secret makes signature/assertion verification trivially forgeable."
            )
        return value

    # Redis-backed (not in-memory — see gateway/middleware.py's docstring for why that matters
    # under multiple replicas), keyed by client IP. Same default jlens's own middleware uses.
    rate_limit_per_minute: int = 100

    # Batch detection
    batch_window_seconds: int = 300    # 5-minute sliding window
    batch_threshold: int = 20          # N failures on same platform → platform-outage batch alert


settings = Settings()
