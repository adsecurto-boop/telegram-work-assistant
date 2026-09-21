"""Deterministic privacy filters for text leaving the local trust boundary."""

import re
from typing import Tuple


REMOTE_TEXT_PATTERNS = (
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE), "[REDACTED_EMAIL]"),
    (re.compile(r"(?<!\w)(?:\+?\d[\d .()-]{8,}\d)(?!\w)"), "[REDACTED_PHONE_OR_ACCOUNT]"),
    (
        re.compile(
            r"\b(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|bearer|password|passcode|secret)"
            r"\s*(?::|=|is)?\s*[\"']?[A-Z0-9_./+=-]{6,}[\"']?",
            re.IGNORECASE,
        ),
        "[REDACTED_CREDENTIAL]",
    ),
    (re.compile(r"\b(?:sk|pk)_[A-Z0-9_-]{12,}\b", re.IGNORECASE), "[REDACTED_TOKEN]"),
)


def redact_for_remote(text: str) -> Tuple[str, int]:
    """Return remote-safe text plus the number of replacements performed."""
    result = text
    replacements = 0
    for pattern, placeholder in REMOTE_TEXT_PATTERNS:
        result, count = pattern.subn(placeholder, result)
        replacements += count
    return result, replacements
