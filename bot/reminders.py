"""DEPRECATED STUB — DO NOT IMPORT.

Shift reminders and tick events are handled by `scheduler.py` via python-telegram-bot job_queue.
This file was an early skeleton and is preserved only for backwards-compatibility.
"""
import warnings

warnings.warn(
    "bot.reminders is deprecated. Use scheduler.py for reminder delivery.",
    DeprecationWarning,
    stacklevel=2,
)


def create_reminder(chat_id, text, when):
    """Placeholder for creating a reminder (deprecated: see scheduler.py:tick)."""
    return {"chat_id": chat_id, "text": text, "when": when}
