"""DEPRECATED STUB — DO NOT IMPORT.

Active command handlers live in `handlers.py` and are registered via `bot.py`.
This file was an early skeleton and is preserved only for backwards-compatibility.
"""
import warnings

warnings.warn(
    "bot.commands is deprecated. Use handlers.py for command implementations.",
    DeprecationWarning,
    stacklevel=2,
)


def start(update, context):
    """Start command handler placeholder (deprecated: see handlers.py:start)."""
    return "started"
