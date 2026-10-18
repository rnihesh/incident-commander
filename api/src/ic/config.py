"""Runtime configuration, read from IC_* environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IC_", env_file=".env", extra="ignore")

    falkordb_url: str = "falkor://localhost:16379"

    llm_provider: str = "openai"
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    embedding_provider: str = "auto"  # auto | hashing | openai (see world/embed.py)
    embedding_model: str = "text-embedding-3-small"
    embedding_dims: int = 512

    github_token: str | None = None
    github_repo: str | None = None
    discord_webhook_url: str | None = None
    alert_hmac_secret: str | None = None
    # Bearer token for the read-only admin usage panel (/api/admin/*, /admin). Empty: the
    # admin routes answer 404.
    admin_token: str = ""

    web_origin: str = "http://localhost:3001"
    # Session cookie (KTD10): Secure is on by default; turn it off for plain-http localhost.
    cookie_secure: bool = True
    cookie_domain: str | None = None  # e.g. ".example.com" to share across app./api.
    # Peers whose X-Forwarded-For is believed (reverse proxies in front of the API).
    trusted_proxies: list[str] = ["127.0.0.1"]
    sse_ping_s: float = 15.0
    cleanup_interval_s: float = 60.0

    max_sessions: int = 8
    # Abuse limits (KTD10). Relax via IC_* env vars for local development and recording.
    session_runs_per_hour: int = 3
    ip_concurrent_sessions: int = 2
    ip_sessions_per_hour: int = 6
    ip_runs_per_hour: int = 6
    daily_usd_breaker: float = 5.0
    actions_per_hour: int = 20
    global_runs_per_hour: int = 60
    # Public proof page (/proof/{ref}) re-verify: per-IP hourly cap and per-ref debounce.
    proof_verifies_per_ip_hour: int = 12
    proof_rerun_interval_s: float = 60.0
    # Session Verify (/api/incidents/{iid}/verify): per-session hourly cap and per-incident
    # debounce (each Verify builds two world copies).
    session_verifies_per_hour: int = 30
    session_verify_interval_s: float = 3.0
    # Judge fragility map (/api/judge/{iid}/fragility): up to ten snapshot copies per call,
    # so a per-session hourly cap and a per-incident debounce.
    session_fragility_per_hour: int = 10
    session_fragility_interval_s: float = 10.0
    # Ask the graph (/api/ask): questions per session per hour (each may spend LLM tokens).
    session_asks_per_hour: int = 20
    session_idle_ttl_s: int = 1800
    # USD per 1M [input, output] tokens (Claude rows: Anthropic list prices, 2026-10).
    # Unknown models fall back to the deliberately high default so the breaker trips early.
    llm_prices: dict[str, list[float]] = {
        "default": [5.0, 20.0],
        "gpt-6-luna": [0.10, 0.50],
        "claude-haiku-5-5": [0.10, 0.50],
        "claude-sonnet-5-5": [2.0, 10.0],
        "claude-opus-5-5": [4.0, 20.0],
        "claude-fable-5-1": [10.0, 50.0],
    }
    # Per-request long-prompt rates: model -> (prompt-token threshold, [input, output]).
    # A request whose prompt exceeds the threshold is priced entirely at the higher rate.
    llm_long_prompt_prices: dict[str, tuple[int, list[float]]] = {
        "claude-haiku-5-5": (100_000, [0.50, 2.50]),
    }

    store_path: str = "./data/ic.sqlite3"
    postgres_dsn: str = "postgresql://localhost:5432/ic"


@lru_cache
def get_settings() -> Settings:
    return Settings()
