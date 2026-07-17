"""Tests for the mandatory log-scrubbing filter (SPEC SECTION 8.18)."""

from __future__ import annotations

from app.core.logging import scrub_text, scrub_value


def test_redacts_jwt() -> None:
    token = "eyJhbGciOi.eyJzdWIiOiIx.sig-part-here"
    assert token not in scrub_text(f"auth used {token}")


def test_redacts_bearer_header() -> None:
    assert "abc123" not in scrub_text("Authorization: Bearer abc123.def")


def test_masks_saudi_phone() -> None:
    assert scrub_text("otp for +966501234567 sent") == "otp for +9665•••••67 sent"


def test_redacts_long_id_digits() -> None:
    assert "1234567890" not in scrub_text("national id 1234567890")


def test_scrub_value_redacts_sensitive_keys() -> None:
    scrubbed = scrub_value({"otp": "849201", "national_id": "123", "city": "Jeddah"})
    assert scrubbed["otp"] == "***REDACTED***"
    assert scrubbed["national_id"] == "***REDACTED***"
    assert scrubbed["city"] == "Jeddah"


def test_scrub_value_recurses() -> None:
    scrubbed = scrub_value({"outer": {"jwt": "x", "keep": "ok"}})
    assert scrubbed["outer"]["jwt"] == "***REDACTED***"
    assert scrubbed["outer"]["keep"] == "ok"
