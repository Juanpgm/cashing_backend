"""Configuration for the SECOP II scraper microservice."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven config."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Shared secret with the main API. The /scrape/* endpoints reject calls
    # whose `X-Internal-Token` header doesn't match.
    SECOP_SCRAPER_INTERNAL_TOKEN: str = "change-me-in-prod"

    # Where to persist Playwright storage state (cookies, local storage).
    SECOP_STORAGE_STATE_PATH: str = "/data/storage_state.json"

    # Run browser in headless mode (true in prod, false locally to debug).
    SECOP_HEADLESS: bool = True

    # Per-request timeout (ms) for navigation/wait steps.
    SECOP_NAV_TIMEOUT_MS: int = 30_000

    # Max captcha retries before giving up with 503.
    SECOP_CAPTCHA_MAX_RETRIES: int = 3


settings = Settings()
