"""JSON logging with trace correlation (spec §15.3).

Every record is one JSON object with ``trace_id`` and ``span_id`` taken from the active OTel span,
so a log line can be joined to its trace. Never log raw evidence, prompts or fixture text (§15.6):
callers pass identifiers and counts, not content.
"""

import json
import logging
import sys
from datetime import UTC, datetime

from opentelemetry import trace

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service = service_name

    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        body: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": format(ctx.trace_id, "032x") if ctx.is_valid else None,
            "span_id": format(ctx.span_id, "016x") if ctx.is_valid else None,
        }
        extras = {k: v for k, v in vars(record).items() if k not in _RESERVED}
        if extras:
            body["extra"] = extras
        if record.exc_info:
            body["exc"] = self.formatException(record.exc_info)
        return json.dumps(body, default=str, ensure_ascii=False)


def configure_logging(service_name: str, level: str = "INFO") -> None:
    """Route the root logger (and uvicorn's) to one JSON stream on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
