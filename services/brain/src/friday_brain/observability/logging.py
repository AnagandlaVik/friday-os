import logging
import sys

import structlog
from structlog.types import Processor


def setup_logging(log_level: str = "INFO") -> None:
    """
    Set up structured logging using structlog.
    """
    log_level = log_level.upper()

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # These run after "wrap_for_formatter" is seen
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Disable uvicorn access logs
    logging.getLogger("uvicorn.access").disabled = True

    # Set the root logger for our application
    logger = structlog.get_logger("friday_brain")
    logger.info("Logging configured", log_level=log_level)
