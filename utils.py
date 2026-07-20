"""
utils.py
--------
Small cross-cutting helpers: logging setup and argument parsing used by
multiple command handlers.
"""

import logging
import logging.handlers
from typing import List, Optional, Tuple

import config


def setup_logging() -> None:
    """
    Configure root logging once at startup:
    - INFO+ to a rotating file (bot.log)
    - matching level to stdout for container/systemd log capture
    """
    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    root = logging.getLogger()
    root.setLevel(level)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(stream_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(file_handler)

    # Quiet down noisy third-party loggers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def parse_int_arg(args: List[str], index: int = 0) -> Optional[int]:
    """Safely parse args[index] as an int, returning None on failure."""
    try:
        return int(args[index])
    except (IndexError, ValueError):
        return None


def split_id_and_reason(args: List[str]) -> Tuple[Optional[int], str]:
    """
    Used by /block <task_id> <reason...>.
    Returns (task_id_or_None, reason_string).
    """
    if not args:
        return None, ""
    task_id = parse_int_arg(args, 0)
    reason = " ".join(args[1:]).strip()
    return task_id, reason


def join_args(args: List[str]) -> str:
    """Used by /add <task text...>."""
    return " ".join(args).strip()