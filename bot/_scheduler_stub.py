"""DEPRECATED STUB — DO NOT IMPORT.

This was an early prototype placeholder. The actual production scheduler lives in
the root `scheduler.py` module. This file is retained only to avoid breaking legacy
references, but should never be used in active application code.
"""
import warnings

warnings.warn(
    "bot._scheduler_stub is deprecated. Use the root scheduler module instead.",
    DeprecationWarning,
    stacklevel=2,
)

from threading import Timer

_scheduled = []


def schedule(delay, fn, *args, **kwargs):
    """Deprecated: Use python-telegram-bot job_queue via root scheduler.py."""
    t = Timer(delay, fn, args=args, kwargs=kwargs)
    t.start()
    _scheduled.append(t)
    return t
