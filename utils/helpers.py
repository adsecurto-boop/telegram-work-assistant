"""DEPRECATED STUB — DO NOT IMPORT.

ISO timestamp utilities and formatters are standardized in `database.py:now_iso` and
application models. This file is preserved only for backwards-compatibility.
"""
import warnings

warnings.warn(
    "utils.helpers is deprecated. Use standard helpers in database.py or standard library.",
    DeprecationWarning,
    stacklevel=2,
)


def format_datetime(dt):
    """Simple datetime formatting helper placeholder (deprecated)."""
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
