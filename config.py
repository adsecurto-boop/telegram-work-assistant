"""
config.py
---------
Centralised configuration for the Telegram Work Assistant Bot.
All values are loaded from environment variables (via a .env file in
development) so no secrets are hard-coded in source.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env file from the project root (no-op if it doesn't exist,
# e.g. in production where real env vars are injected by the host).
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# --- Telegram -----------------------------------------------------------
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# --- Database -------------------------------------------------------------
DB_PATH: str = os.getenv("DB_PATH", str(BASE_DIR / "tasks.db"))

# --- Scheduler ------------------------------------------------------------
# How often (in hours) pending-task reminders are sent.
REMINDER_INTERVAL_HOURS: int = int(os.getenv("REMINDER_INTERVAL_HOURS", "4"))

# Timezone used by APScheduler for all scheduled jobs.
TIMEZONE: str = os.getenv("TIMEZONE", "UTC")

# Toggle reminders on/off without touching code.
REMINDERS_ENABLED: bool = _get_bool("REMINDERS_ENABLED", True)

# --- Logging ----------------------------------------------------------------
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = os.getenv("LOG_FILE", str(BASE_DIR / "bot.log"))


def validate_config() -> None:
    """Fail fast if required configuration is missing."""
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not set. Create a .env file (see .env.example) "
            "and set BOT_TOKEN=<your token from @BotFather>."
        )