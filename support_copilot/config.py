import os
import json
from pathlib import Path
from typing import Any, Dict, List
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
RECOGNIZED_CAPABILITIES = {
    "capture:write",
    "capture:read",
    "read:only",
    "knowledge:read",
    "knowledge:write",
    "knowledge:approve",
    "suggestion:read",
    "suggestion:write",
    "response:confirm_sent",
    "activity:read",
    "activity:write",
    "report:read",
    "report:write",
    "case:read",
    "case:write",
}
DISALLOWED_PLACEHOLDERS = {
    "<generate-a-private-random-token>",
    "generate-a-private-random-token",
    "<generate-a-private-token>",
    "local-dev-token",
    "test-token",
    "placeholder",
    "changeme",
    "your-token-here",
    "admin",
    "password",
    "secret",
}

def resolve_and_validate_db_path(db_path: str, data_dir: str) -> str:
    """
    Validates that the database path:
    1. Uses the 'sqlite:///' URI scheme.
    2. Does not target Telegram assistant database ('storage/work.sqlite3') or workspace root.
    3. Resolves to a canonical absolute path strictly inside data_dir.
    4. Is not a directory or empty string.
    """
    if not db_path or not db_path.strip():
        raise ValueError("Database path cannot be empty.")
    if not db_path.startswith("sqlite:///"):
        raise ValueError(f"Unsupported database URI '{db_path}'. Only 'sqlite:///' is supported.")

    raw_path = db_path.replace("sqlite:///", "").strip()
    if not raw_path:
        raise ValueError("Database path cannot be empty.")

    clean_db_path = os.path.abspath(raw_path)
    clean_data_dir = os.path.abspath(data_dir)

    # Explicitly check for Telegram assistant database first
    base_name = os.path.basename(clean_db_path).lower()
    if "work.sqlite3" in clean_db_path.lower() or "bot.db" in base_name or "telegram_assistant" in clean_db_path.lower():
        raise ValueError("Database path cannot target the Telegram assistant database.")

    # Workspace root check
    repo_root = os.path.abspath(".")
    if os.path.dirname(clean_db_path) == repo_root:
        raise ValueError("Database path cannot target the workspace root directory.")

    # Check if target is a directory
    if os.path.isdir(clean_db_path):
        raise ValueError(f"Database path '{clean_db_path}' cannot be a directory.")

    # Must reside strictly within data_dir
    try:
        common = os.path.commonpath([clean_db_path, clean_data_dir])
        if common != clean_data_dir:
            raise ValueError(
                f"Database path '{clean_db_path}' must reside inside data directory '{clean_data_dir}'."
            )
    except ValueError as exc:
        raise ValueError(f"Database path resolution failed: {exc}") from exc

    return clean_db_path

class Settings(BaseSettings):
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    data_dir: str = "storage/support_copilot"
    db_path: str = "sqlite:///./storage/support_copilot/copilot.sqlite3"
    log_level: str = "INFO"
    environment: str = "development"
    ai_provider: str = "disabled"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    ai_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    n8n_shared_secret: SecretStr | None = None
    n8n_signature_max_age_seconds: int = Field(default=300, ge=30, le=3600)
    api_tokens: Dict[str, List[str]] = Field(default_factory=dict)

    @field_validator("api_host")
    @classmethod
    def validate_loopback_host(cls, v: str) -> str:
        if v.strip().lower() not in ALLOWED_LOOPBACK_HOSTS:
            raise ValueError(
                f"Unsafe bind host '{v}'. Only loopback addresses ({', '.join(sorted(ALLOWED_LOOPBACK_HOSTS))}) are permitted."
            )
        return v

    @field_validator("api_tokens", mode="before")
    @classmethod
    def parse_api_tokens(cls, v: Any) -> Dict[str, List[str]]:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return {}
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                raise ValueError("COPILOT_API_TOKENS must be a valid JSON dictionary of token to capabilities.")
        if isinstance(v, dict):
            return v
        return {}

    @field_validator("ai_provider")
    @classmethod
    def validate_ai_provider(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"disabled", "gemini"}:
            raise ValueError("COPILOT_AI_PROVIDER must be either 'disabled' or 'gemini'.")
        return normalized

    def validate_ai_provider_for_startup(self) -> None:
        if self.ai_provider == "gemini":
            if self.gemini_api_key is None or not self.gemini_api_key.get_secret_value().strip():
                raise ValueError(
                    "COPILOT_GEMINI_API_KEY is required when COPILOT_AI_PROVIDER is 'gemini'."
                )

    def get_canonical_db_path(self) -> str:
        return resolve_and_validate_db_path(self.db_path, self.data_dir)

    def validate_tokens_for_startup(self) -> None:
        """Validates that at least one valid, non-placeholder, scoped API token is configured."""
        if not self.api_tokens:
            raise ValueError(
                "Missing required API authentication configuration. "
                "Configure 'COPILOT_API_TOKENS' environment variable with at least one valid token and capability "
                "(e.g. '{\"<token>\": [\"capture:write\"]}')."
            )
        for token, capabilities in self.api_tokens.items():
            if not isinstance(token, str) or not token.strip():
                raise ValueError("Configured API token cannot be empty.")
            cleaned_token = token.strip()
            lower_token = cleaned_token.lower()
            if (
                lower_token in DISALLOWED_PLACEHOLDERS
                or any(p in lower_token for p in ["placeholder", "changeme", "local-dev-token", "your-token-here", "generate-a-private"])
                or "<" in cleaned_token
                or ">" in cleaned_token
            ):
                raise ValueError(
                    "Configured API token must not use a documented placeholder or default value."
                )
            if len(cleaned_token) < 16:
                raise ValueError("Configured API token must be at least 16 characters long.")
            if not isinstance(capabilities, list) or not capabilities:
                raise ValueError("Configured API token must have at least one recognized capability.")
            for cap in capabilities:
                if cap not in RECOGNIZED_CAPABILITIES:
                    raise ValueError(
                        f"Unknown capability '{cap}'. Allowed capabilities: {', '.join(sorted(RECOGNIZED_CAPABILITIES))}."
                    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="COPILOT_",
        extra="ignore",
    )

def get_settings() -> Settings:
    return Settings()
