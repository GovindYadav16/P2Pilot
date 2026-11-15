"""Application settings, read from environment variables (and .env)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve paths from the code's location, not the current directory, so the API, seeder
# and tests behave the same whether started from the repo root or from backend/.
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # Reads the repo-root .env (wherever the process is started), or a local .env override.
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM gateway (LiteLLM proxy). The app only ever names aliases: fast / reasoning / embed.
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str = "sk-p2pilot-dev"
    llm_fake: bool = False  # deterministic fake model for tests and offline demos
    llm_timeout_s: float = 60.0

    # --- Supabase
    supabase_url: str = "http://127.0.0.1:54321"
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    # HS256 secret (older/local stacks). Leave empty to verify with the project's JWKS instead.
    supabase_jwt_secret: str = ""
    jwt_audience: str = "authenticated"

    # Local development only: skip JWT checks and act as a fixed dev user.
    auth_disabled: bool = False

    cors_origins: list[str] = ["http://localhost:5173"]

    # Local tooling (seeder, integration tests) and server-side DB access. Empty = not set.
    p2pilot_db_url: str = ""
    seed_user_password: str = "P2Pilot!dev123"
    # With AUTH_DISABLED, act as this seeded user (must exist after seeding).
    dev_user_email: str = "clerk@acme.test"

    # --- Files (ADR 0002). "local" = folder on disk; "supabase" = Supabase Storage.
    object_store: str = "local"
    object_store_root: str = str(REPO_ROOT / "data" / "objects")
    object_store_bucket: str = "p2pilot"
    max_upload_mb: int = 10

    # --- Invoice processing
    extractor: str = "llm"                 # "llm" | "rules" (rules is used when llm_fake)
    approval_threshold: float = 200000.0   # INR; invoices above this always need a human
    price_tolerance_pct: float = 2.0

    # --- Run state (LangGraph checkpoints). auto = Postgres when P2PILOT_DB_URL is set.
    checkpointer: str = "auto"             # "auto" | "postgres" | "memory"


@lru_cache
def get_settings() -> Settings:
    return Settings()
