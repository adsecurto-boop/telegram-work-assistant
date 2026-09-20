import logging
import json
import re
from datetime import datetime, timezone
from typing import Any

SECRET_KEY_PATTERN = re.compile(
    r"(token|secret|password|api[_-]?key|auth|cookie|credential|bearer)",
    re.IGNORECASE,
)

SECRET_VALUE_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"(ghp_[a-zA-Z0-9]{36}|sk-[a-zA-Z0-9]{20,}|AIza[0-9A-Za-z-_]{35})"),
]

def redact_string(value: str) -> str:
    redacted = value
    for pattern in SECRET_VALUE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted

def redact_data(data: Any) -> Any:
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            if isinstance(k, str) and SECRET_KEY_PATTERN.search(k):
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = redact_data(v)
        return cleaned
    elif isinstance(data, list):
        return [redact_data(item) for item in data]
    elif isinstance(data, str):
        return redact_string(data)
    return data

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(timezone.utc).isoformat()

        raw_msg = record.getMessage()
        redacted_msg = redact_string(raw_msg)

        log_record = {
            "timestamp": timestamp,
            "level": record.levelname,
            "message": redacted_msg,
            "name": record.name,
        }

        if hasattr(record, "correlation_id"):
            log_record["correlation_id"] = record.correlation_id
        if hasattr(record, "failure_category"):
            log_record["failure_category"] = record.failure_category
        if hasattr(record, "details"):
            log_record["details"] = redact_data(record.details)

        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            log_record["exception"] = redact_string(exc_text)

        return json.dumps(log_record)

def setup_logger(log_level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("support_copilot")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
    logger.setLevel(log_level.upper())
    logger.propagate = False
    return logger

logger = setup_logger()
