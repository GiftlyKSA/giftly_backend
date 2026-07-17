"""Structured JSON logging with a mandatory scrubbing filter (SPEC SECTION 8.17-18).

A log file full of OTPs turns a log-read into a breach, so scrubbing is not optional:
the filter redacts by key name AND by regex before any record is emitted. All logs
carry a ``request_id`` when one is bound by the request middleware.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

# Key names whose values must never be logged (case-insensitive substring match).
_SENSITIVE_KEYS = (
    "otp",
    "password",
    "secret",
    "token",
    "authorization",
    "jwt",
    "refresh",
    "national_id",
    "passport",
    "iban",
    "encryption",
    "api_key",
    "webhook_secret",
    "content_encrypted",
    "session_token",
    "pepper",
)

# Value patterns to redact even when the key is innocuous.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]+")
_PHONE_RE = re.compile(r"\+9665\d{8}")
_LONG_DIGITS_RE = re.compile(r"\b\d{9,}\b")

_REDACTED = "***REDACTED***"


def _mask_phone(match: re.Match[str]) -> str:
    """Mask a Saudi mobile as ``+9665•••••67`` (SPEC SECTION 8.18)."""
    phone = match.group(0)
    return f"{phone[:5]}•••••{phone[-2:]}"


def scrub_value(value: Any) -> Any:
    """Recursively redact sensitive keys and value patterns in a log payload."""
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _is_sensitive_key(k) else scrub_value(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub_value(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in _SENSITIVE_KEYS)


def scrub_text(text: str) -> str:
    """Redact JWTs, bearer tokens, long id-like digit runs; mask phone numbers."""
    text = _JWT_RE.sub(_REDACTED, text)
    text = _BEARER_RE.sub(_REDACTED, text)
    text = _PHONE_RE.sub(_mask_phone, text)
    text = _LONG_DIGITS_RE.sub(_REDACTED, text)
    return text


class ScrubbingJsonFormatter(logging.Formatter):
    """Emits one scrubbed JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        """Render a scrubbed JSON log line."""
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": scrub_text(record.getMessage()),
        }
        request_id = getattr(record, "request_id", None)
        if request_id is not None:
            payload["request_id"] = request_id
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload["fields"] = scrub_value(extra)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the scrubbing JSON formatter as the root handler."""
    handler = logging.StreamHandler()
    handler.setFormatter(ScrubbingJsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
