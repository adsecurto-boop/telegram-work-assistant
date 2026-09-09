"""
Input normalization layer for natural-language interpretation.
Normalizes whitespace, Unicode characters, common work shorthands, and time separators
while strictly protecting client names, ticket IDs, URLs, emails, versions, and quoted content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizedInput:
    raw_text: str
    normalized_text: str
    lowered_text: str
    tokens: list[str] = field(default_factory=list)
    protected_spans: list[dict[str, Any]] = field(default_factory=list)
    detected_modals: list[str] = field(default_factory=list)
    has_negation: bool = False


# Modal and uncertainty expressions
MODAL_PHRASES = (
    'can be',
    'could be',
    'may be',
    'might be',
    'should be',
    'might work',
    'might take',
    'could work',
    'maybe',
    'probably',
    'possibly',
    'perhaps',
)

NEGATION_PATTERN = re.compile(
    r"(?:^\s*(?:please\s+)?(?:do\s+not|don't|dont|never|cancel|stop)\b|"
    r"\b(?:do\s+not|don't|dont)\s+(?:change|set|mark|complete|create|move|carry|delete|close|start|schedule)\b)",
    re.IGNORECASE
)

# Regex patterns for protected entities
PROTECTED_PATTERNS = [
    ('url', re.compile(r'https?://[^\s<>"]+|www\.[^\s<>"]+', re.IGNORECASE)),
    ('email', re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')),
    ('quoted_double', re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')),
    ('quoted_single', re.compile(r"'([^'\\]*(?:\\.[^'\\]*)*)'")),
    ('ticket', re.compile(r'\b[A-Z]{2,10}-\d+\b')),
    ('version', re.compile(r'\bv?\d+\.\d+(?:\.\d+)?(?:-[A-Za-z0-9.]+)?\b', re.IGNORECASE)),
]


def normalize_input(raw_text: str) -> NormalizedInput:
    """
    Normalizes user input conservatively, preserving the original string and
    protecting specific technical spans.
    """
    if not raw_text:
        return NormalizedInput(
            raw_text='',
            normalized_text='',
            lowered_text='',
            tokens=[],
            protected_spans=[],
            detected_modals=[],
            has_negation=False
        )

    # 1. Identify protected spans from raw text
    spans: list[dict[str, Any]] = []
    # Mask used during normalization
    masked_text = raw_text
    placeholders: dict[str, str] = {}

    for p_type, pattern in PROTECTED_PATTERNS:
        for match in pattern.finditer(masked_text):
            val = match.group(0)
            token = f"__PROTECTED_{len(placeholders)}__"
            placeholders[token] = val
            spans.append({
                'type': p_type,
                'value': val,
                'start': match.start(),
                'end': match.end()
            })

    # Substitute placeholders to shield technical spans
    for token, val in placeholders.items():
        masked_text = masked_text.replace(val, token, 1)

    # 2. Normalize Unicode apostrophes, quotes, and dashes
    # Unicode single quotes / apostrophes -> '
    s = re.sub(r"[\u2018\u2019\u201a\u201b\u0060\u00b4]", "'", masked_text)
    # Unicode dashes -> -
    s = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]", "-", s)

    # 3. Normalize repeated whitespace
    s = re.sub(r'\s+', ' ', s).strip()

    # 4. Command spacing: e.g. "/understand    foo" -> "/understand foo"
    s = re.sub(r'^(/[\w]+)\s+', r'\1 ', s)

    # 5. Date slang and variations (word boundaries)
    # tomorrow variants
    s = re.sub(r"\b(?:tmrw|tomorow|tommorrow|tmr)\b", "tomorrow", s, flags=re.IGNORECASE)
    # today's variants: "todays", "today s" -> "today's"
    s = re.sub(r"\btodays\b", "today's", s, flags=re.IGNORECASE)
    s = re.sub(r"\btoday\s+s\b", "today's", s, flags=re.IGNORECASE)

    # 6. Time range separators: 12-9, 12–9, 12 - 9 -> "12 to 9"
    # Match digits/(am/pm) followed by optional spaces, dash, optional spaces, and digits/(am/pm)
    s = re.sub(
        r'(\b\d{1,2}(?::\d{2})?(?:am|pm)?)\s*-\s*(\d{1,2}(?::\d{2})?(?:am|pm)?\b)',
        r'\1 to \2',
        s,
        flags=re.IGNORECASE
    )
    s = re.sub(r'\s+', ' ', s).strip()

    # 7. Common work abbreviations capitalization and formatting
    # E.g. "tod" as standalone word -> "TOD", "eod" -> "EOD"
    s = re.sub(r'\b(?:start\s+of\s+day|beginning\s+of\s+day)\b', 'TOD', s, flags=re.IGNORECASE)
    s = re.sub(r'\bend\s+of\s+day\b', 'EOD', s, flags=re.IGNORECASE)
    s = re.sub(r'\bpre[- ]lunch\b', 'PL', s, flags=re.IGNORECASE)

    # 8. Restore protected placeholders
    for token, val in placeholders.items():
        s = s.replace(token, val)

    # 9. Lowercase version for search / matching
    lowered = s.casefold()

    # 10. Detect modal verbs and negation
    detected_modals = []
    for phrase in MODAL_PHRASES:
        pattern = rf'\b{re.escape(phrase)}\b'
        if re.search(pattern, lowered):
            detected_modals.append(phrase)

    has_neg = bool(NEGATION_PATTERN.search(lowered))

    # Token list
    tokens = s.split()

    return NormalizedInput(
        raw_text=raw_text,
        normalized_text=s,
        lowered_text=lowered,
        tokens=tokens,
        protected_spans=spans,
        detected_modals=detected_modals,
        has_negation=has_neg
    )
