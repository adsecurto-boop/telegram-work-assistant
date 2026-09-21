from support_copilot.privacy import redact_for_remote


def test_remote_text_redaction_removes_personal_and_credential_values():
    raw = (
        "Email user@example.com or phone +91 98765 43210. "
        "Account 1234 5678 9012 and API key=ABCDEF1234567890 must stay private."
    )
    safe, count = redact_for_remote(raw)
    assert count == 4
    assert "user@example.com" not in safe
    assert "98765" not in safe
    assert "1234 5678" not in safe
    assert "ABCDEF1234567890" not in safe
    assert "[REDACTED_EMAIL]" in safe
    assert "[REDACTED_CREDENTIAL]" in safe


def test_remote_text_redaction_preserves_non_sensitive_troubleshooting_context():
    raw = "Attendance logs are missing for the selected date range after nightly ingestion."
    safe, count = redact_for_remote(raw)
    assert safe == raw
    assert count == 0
