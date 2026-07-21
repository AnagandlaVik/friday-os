import json
import logging
import sys

from friday_brain.observability.logging import (
    redact_sensitive_data,
    sanitize_exception_info,
    setup_logging,
)


def test_sensitive_fields_are_recursively_redacted() -> None:
    event = {
        "event": "tool.failed",
        "task_id": "task-123",
        "prompt": "private prompt",
        "arguments": {
            "path": "secret.txt",
            "content": "private content",
        },
        "headers": {
            "Authorization": ("Bearer private-token"),
            "Cookie": "session=private",
            "X-API-Key": "private-key",
        },
        "nested": [
            {
                "password": "hunter2",
                "safe": "visible",
            }
        ],
    }

    redacted = redact_sensitive_data(
        None,
        "info",
        event,
    )
    rendered = json.dumps(redacted)

    assert redacted["task_id"] == "task-123"
    assert "[REDACTED]" in rendered

    for secret in (
        "private prompt",
        "private content",
        "private-token",
        "session=private",
        "private-key",
        "hunter2",
    ):
        assert secret not in rendered


def test_inline_credentials_are_scrubbed() -> None:
    event = {
        "event": ("Request used Bearer abc.def.ghi and password=hunter2"),
        "database": ("postgresql://user:private-password@localhost/db"),
    }

    redacted = redact_sensitive_data(
        None,
        "error",
        event,
    )
    rendered = json.dumps(redacted)

    assert "abc.def.ghi" not in rendered
    assert "hunter2" not in rendered
    assert "private-password" not in rendered
    assert "[REDACTED]" in rendered


def test_exception_message_is_not_rendered() -> None:
    try:
        raise ValueError("private prompt must never be logged")
    except ValueError:
        event = {
            "event": "operation.failed",
            "exc_info": sys.exc_info(),
        }

    sanitized = sanitize_exception_info(
        None,
        "error",
        event,
    )
    rendered = json.dumps(
        sanitized,
        default=str,
    )

    assert sanitized["exception_type"] == "ValueError"
    assert "private prompt" not in rendered
    assert "exc_info" not in sanitized


def test_setup_logging_is_idempotent() -> None:
    setup_logging("INFO")
    setup_logging("INFO")

    friday_handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(
            handler,
            "_friday_structured_handler",
            False,
        )
    ]

    assert len(friday_handlers) == 1
