"""Central settings — loaded once at startup."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENV: str = "development"
    GCP_PROJECT_ID: str = ""
    GCS_BUCKET_NAME: str = "job-search-agent-bucket"
    KMS_KEY_NAME: str = ""

    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    OAUTH_REDIRECT_URI: str = "http://localhost:8080/auth/google/callback"

    # Defaults that users can override per-run
    DEFAULT_MIN_SCORE: int = 40
    DEFAULT_MAX_RESULTS: int = 20
    DEFAULT_BATCH_SIZE: int = 10
    SIGNED_URL_EXPIRY_MINUTES: int = 15


def get_settings() -> Settings:
    return Settings()
