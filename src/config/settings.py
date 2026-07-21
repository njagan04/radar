from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: str  # no longer used by investigator.py/classifier.py — see azure_openai_v1_base_url
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
    rerun_freshness_window_seconds: int = 7200

    nexus_base_url: str = "http://localhost:8000"

    hmac_secret: str | None = None

    # Approval flow
    approval_timeout_hours: int = 24

    # Rerun outcome polling
    rerun_outcome_check_interval_seconds: int = 300  # 5 min between checks
    rerun_outcome_max_checks: int = 3                # up to 15 min total

    # Denial escalation
    denial_threshold: int = 3  # override known-fix to full investigation after N denials

    # Batch detection
    batch_window_seconds: int = 300    # 5-minute sliding window
    batch_threshold: int = 20          # N failures on same platform → platform-outage batch alert


settings = Settings()
