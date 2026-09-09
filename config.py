"""Local configuration; secrets stay in .env."""
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from credential_store import configured_secret
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')
BOT_TOKEN = configured_secret('BOT_TOKEN')
_OWNER_ID_TEXT = os.getenv('OWNER_ID', '').strip()
OWNER_ID = int(_OWNER_ID_TEXT) if _OWNER_ID_TEXT.isdigit() else 0
LEGACY_PATH = os.getenv('DB_PATH', str(BASE_DIR / 'storage/tasks.json'))
DB_PATH = os.getenv('SQLITE_PATH', str(BASE_DIR / 'storage/work.sqlite3'))
TIMEZONE = os.getenv('SHIFT_TIMEZONE', 'Asia/Kolkata')
REMINDERS_ENABLED = os.getenv('REMINDERS_ENABLED', 'true').lower() in ('true','1','yes')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FILE = str(BASE_DIR / 'bot.log')
AI_PROVIDER = os.getenv('AI_PROVIDER', 'gemini')
AI_MODEL = os.getenv('AI_MODEL', '')
AI_FALLBACK_MODEL = os.getenv('AI_FALLBACK_MODEL', 'gemini-3.5-flash-lite')
AI_KEY = configured_secret('GEMINI_API_KEY')
AI_DAILY_LIMIT = int(os.getenv('AI_DAILY_LIMIT', '30'))
BACKUP_INTERVAL_HOURS = int(os.getenv('BACKUP_INTERVAL_HOURS', '6'))
BACKUP_RETENTION = int(os.getenv('BACKUP_RETENTION', '30'))
DASHBOARD_ENABLED = os.getenv('DASHBOARD_ENABLED', 'true').lower() in ('true','1','yes')
DASHBOARD_HOST = '127.0.0.1'
DASHBOARD_PORT = int(os.getenv('DASHBOARD_PORT', '8765'))
MEDIA_RETENTION_DAYS = int(os.getenv('MEDIA_RETENTION_DAYS', '90'))
DELETE_VOICE_AFTER_TRANSCRIPTION = os.getenv(
    'DELETE_VOICE_AFTER_TRANSCRIPTION', 'true').lower() in ('true','1','yes')
FRESHDESK_DOMAIN = os.getenv('FRESHDESK_DOMAIN', '')
FRESHDESK_API_KEY = configured_secret('FRESHDESK_API_KEY')
FRESHCHAT_BASE_URL = os.getenv('FRESHCHAT_BASE_URL', '')
FRESHCHAT_API_KEY = configured_secret('FRESHCHAT_API_KEY')
ENABLED_CONNECTORS = tuple(value.strip().casefold() for value in
                           os.getenv('ENABLED_CONNECTORS', '').split(',') if value.strip())
CONNECTOR_SYNC_MINUTES = int(os.getenv('CONNECTOR_SYNC_MINUTES', '15'))

def validate_bot_token(token):
    """Reject pasted control characters and obviously malformed BotFather tokens."""
    if not token:
        raise ValueError('Telegram bot token is missing.')
    if not token.isascii() or not token.isprintable() or any(ch.isspace() for ch in token):
        raise ValueError(
            'Telegram bot token contains a hidden or whitespace character. '
            'Run scripts\\configure.py again and right-click to paste into the hidden prompt; '
            'Ctrl+V may insert a control character in some Windows terminals.'
        )
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', token):
        raise ValueError('Telegram bot token format is invalid. Copy the complete token from @BotFather.')

def validate_config():
    try:
        validate_bot_token(BOT_TOKEN)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if OWNER_ID <= 0:
        raise RuntimeError(
            'OWNER_ID must be your numeric Telegram user ID, not your phone number. '
            'Use @userinfobot in Telegram to find it, then rerun scripts\\configure.py.'
        )
    ZoneInfo(TIMEZONE)
    if Path(DB_PATH).resolve() == Path(LEGACY_PATH).resolve():
        raise RuntimeError('SQLITE_PATH must differ from legacy JSON DB_PATH.')
    if AI_DAILY_LIMIT < 1:
        raise RuntimeError('AI_DAILY_LIMIT must be positive.')
    if BACKUP_INTERVAL_HOURS < 1 or BACKUP_RETENTION < 2:
        raise RuntimeError('BACKUP_INTERVAL_HOURS must be positive and BACKUP_RETENTION at least 2.')
    if not 1024 <= DASHBOARD_PORT <= 65535:
        raise RuntimeError('DASHBOARD_PORT must be between 1024 and 65535.')
    if MEDIA_RETENTION_DAYS < 1:
        raise RuntimeError('MEDIA_RETENTION_DAYS must be positive.')
    if CONNECTOR_SYNC_MINUTES < 5:
        raise RuntimeError('CONNECTOR_SYNC_MINUTES must be at least 5.')
