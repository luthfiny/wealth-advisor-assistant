"""JSON events without raw client inputs, transactions or freeform review notes."""

import json
import logging
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "event": record.getMessage(),
                **getattr(record, "fields", {}),
            }
        )


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("wealth_advisor")
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False


def emit(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    logging.getLogger("wealth_advisor").log(level, event, extra={"fields": fields})
