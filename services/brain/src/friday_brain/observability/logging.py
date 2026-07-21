import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from structlog.types import Processor


_REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "apikey",
        "xapikey",
        "accesstoken",
        "refreshtoken",
        "token",
        "password",
        "passwd",
        "secret",
        "clientsecret",
        "prompt",
        "input",
        "content",
        "arguments",
        "body",
        "payload",
        "headers",
        "requestbody",
        "responsebody",
        "output",
        "result",
    }
)

_INLINE_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\bBearer\s+"
        r"[A-Za-z0-9._~+/=-]+"
    ),
    re.compile(
        r"(?i)\b("
        r"api[_-]?key|access[_-]?token|"
        r"refresh[_-]?token|password|"
        r"passwd|secret"
        r")\s*[:=]\s*[^\s,;]+"
    ),
)

_URL_CREDENTIAL_PATTERN = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)"
    r"(?P<username>[^:/@\s]+):"
    r"(?P<password>[^@\s]+)@"
)


def _normalize_key(key: object) -> str:
    return "".join(
        character for character in str(key).casefold() if character.isalnum()
    )


def _scrub_string(value: str) -> str:
    scrubbed = _URL_CREDENTIAL_PATTERN.sub(
        (
            r"\g<scheme>\g<username>:"
            f"{_REDACTED}@"
        ),
        value,
    )

    scrubbed = _INLINE_SECRET_PATTERNS[0].sub(
        f"Bearer {_REDACTED}",
        scrubbed,
    )

    scrubbed = _INLINE_SECRET_PATTERNS[1].sub(
        lambda match: f"{match.group(1)}={_REDACTED}",
        scrubbed,
    )

    return scrubbed


def _redact_value(
    value: Any,
    *,
    key: object | None = None,
) -> Any:
    if key is not None and _normalize_key(key) in _SENSITIVE_KEYS:
        return _REDACTED

    if isinstance(value, Mapping):
        return {
            str(nested_key): _redact_value(
                nested_value,
                key=nested_key,
            )
            for nested_key, nested_value in value.items()
        }

    if isinstance(
        value,
        (list, tuple, set, frozenset),
    ):
        return [_redact_value(item) for item in value]

    if isinstance(value, str):
        return _scrub_string(value)

    return value


def sanitize_exception_info(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> Mapping[str, Any]:
    """
    Keep exception type information without rendering exception
    messages or tracebacks that may contain sensitive values.
    """
    del logger
    del method_name

    exc_info = event_dict.pop(
        "exc_info",
        None,
    )
    event_dict.pop("stack_info", None)
    event_dict.pop("stack", None)
    event_dict.pop("exception", None)

    if (
        isinstance(exc_info, tuple)
        and len(exc_info) >= 1
        and isinstance(exc_info[0], type)
    ):
        event_dict.setdefault(
            "exception_type",
            exc_info[0].__name__,
        )
    elif exc_info:
        event_dict.setdefault(
            "exception_recorded",
            True,
        )

    return event_dict


def redact_sensitive_data(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> Mapping[str, Any]:
    """Recursively redact secrets and user/tool payload fields."""
    del logger
    del method_name

    return {
        str(key): _redact_value(
            value,
            key=key,
        )
        for key, value in event_dict.items()
    }


def setup_logging(
    log_level: str = "INFO",
) -> None:
    """Configure safe structured JSON logging."""
    normalized_level = log_level.upper()

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(
            fmt="iso",
            utc=True,
        ),
        sanitize_exception_info,
        redact_sensitive_data,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            (structlog.stdlib.ProcessorFormatter.wrap_for_formatter),
        ],
        logger_factory=(structlog.stdlib.LoggerFactory()),
        wrapper_class=(structlog.stdlib.BoundLogger),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            (structlog.stdlib.ProcessorFormatter.remove_processors_meta),
            structlog.processors.JSONRenderer(default=str),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    setattr(
        handler,
        "_friday_structured_handler",
        True,
    )

    root_logger = logging.getLogger()

    # create_app() is called repeatedly in tests. Replace only the
    # handler owned by FRIDAY instead of stacking duplicate handlers.
    for existing in list(root_logger.handlers):
        if getattr(
            existing,
            "_friday_structured_handler",
            False,
        ):
            root_logger.removeHandler(existing)
            existing.close()

    root_logger.addHandler(handler)
    root_logger.setLevel(normalized_level)

    logging.getLogger("uvicorn.access").disabled = True

    logger = structlog.get_logger("friday_brain")
    logger.info(
        "logging.configured",
        configured_log_level=(normalized_level),
    )
