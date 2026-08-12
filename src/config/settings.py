from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: (
        str  # not used by the chat agent — see azure_openai_v1_base_url
    )
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

    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/watchtower"
    )
    # Which Postgres schema inside that database holds RADAR's tables. main.py's engine sets
    # this as the connection's search_path via connect_args.
    radar_db_schema: str = "radar"
    redis_url: str = "redis://localhost:6379"

    # Postgres connection pool (main.py's create_async_engine) — per PROCESS, not per replica:
    # with N replicas x M worker processes each, the real total against this (WatchTower-shared)
    # database is N * M * (db_pool_size + db_max_overflow). Tune against Postgres's own
    # max_connections and whatever WatchTower's own backend already holds, not in isolation.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_timeout_seconds: float = 30.0
    # Proactively refreshes connections older than this — many managed Postgres services
    # enforce their own idle/max-lifetime limits server-side; recycling client-side first
    # avoids depending solely on pool_pre_ping to catch every case.
    db_pool_recycle_seconds: int = 1800
    # Cheap liveness check before handing out a pooled connection. Without this, a connection
    # that silently died (idle-killed, network blip, DB restart) surfaces as a raw "server
    # closed the connection unexpectedly" error on whatever request happens to use it next,
    # instead of being caught and transparently replaced first.
    db_pool_pre_ping: bool = True

    redis_max_connections: int = 20
    redis_socket_timeout_seconds: float = 5.0

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

    # gateway/credential_resolution.py reads every project's client_secret straight from
    # WatchTower's own public."Credential".clientSecret, decrypted with this same passphrase
    # WatchTower's lib/crypto.ts::decryptData uses (must match its JWT_SECRET_KEY exactly).
    watchtower_credential_key: str | None = None

    # Gates FastAPI's interactive docs (main.py) — "production" disables /docs, /redoc, and the
    # raw /openapi.json; anything else (the default) leaves them on for local/dev use.
    environment: str = "development"

    # Verifies the X-Radar-Assertion header WatchTower's proxy sends on every chat call
    # (chat/deps.py) — a short-lived HS256 JWT proving a real, SSO-verified WatchTower user is
    # behind the request, minted by WatchTower's own backend with this same shared secret.
    # Required: an unset/empty secret must not silently disable verification.
    radar_assertion_secret: str

    # Required, no default — an empty-string HMAC key would be trivially forgeable by anyone,
    # so the app refuses to start without a real, non-empty secret configured. Same validator
    # covers radar_assertion_secret.
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

    # Redis-backed (shared across replicas), keyed by client IP.
    rate_limit_per_minute: int = 100

    # Batch detection
    batch_window_seconds: int = 300  # 5-minute sliding window
    batch_threshold: int = (
        20  # N failures on same platform → platform-outage batch alert
    )


settings = Settings()
