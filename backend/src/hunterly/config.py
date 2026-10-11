"""Settings, read from environment variables prefixed HUNTERLY_ (or a .env file)."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HUNTERLY_", env_file=".env", extra="ignore")

    # Where the SQLite file, Gmail token and install secret live.
    data_dir: Path = Path.home() / ".hunterly"

    # Web server (bind to localhost only).
    host: str = "127.0.0.1"
    port: int = 8765

    # Guardrails from the PRD.
    daily_capture_cap: int = 30
    daily_first_email_cap: int = 20
    max_messages_per_contact: int = 3
    followup_days: list[int] = [4, 9]  # days after the first email for message 2 and 3

    # Mail-server (SMTP) verification. Run locally, or point at the verifier VM.
    smtp_helo_host: str = "localhost"
    smtp_mail_from: str = ""  # defaults to verify@<smtp_helo_host>
    smtp_timeout: float = 10.0
    smtp_checks_per_domain_per_hour: int = 60
    max_checks: int = 10  # how many of the top guesses to check with the mail server
    verifier_url: str | None = None  # e.g. http://127.0.0.1:8766 through an SSH tunnel
    verifier_key: str | None = None

    # Hunter.io (optional backup checker; free plan has 50 credits a month). Can also be set
    # on the Settings page.
    hunter_api_key: str | None = None

    # Local LLM for drafts.
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2:3b"
    ollama_timeout: float = 120.0

    # Gmail API (optional; without it the app opens Gmail's compose window instead).
    gmail_credentials_file: Path | None = None

    @field_validator("data_dir", "gmail_credentials_file")
    @classmethod
    def _expand_home(cls, v: Path | None) -> Path | None:
        # "~/.hunterly" in .env should mean the home folder on every OS (Windows included)
        return v.expanduser() if v is not None else v

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hunterly.db"

    @property
    def mail_from(self) -> str:
        return self.smtp_mail_from or f"verify@{self.smtp_helo_host}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
