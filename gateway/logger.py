import logging
import sys

from opentelemetry import trace as otel_trace
from pythonjsonlogger import jsonlogger


class _TraceContextFilter(logging.Filter):
    """Injects active OTel trace_id and span_id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = otel_trace.get_current_span().get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else ""
        record.span_id = format(ctx.span_id, "016x") if ctx.is_valid else ""
        return True


def _setup() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_TraceContextFilter())
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            fmt="%(asctime)s %(name)s %(levelname)s %(message)s %(trace_id)s %(span_id)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    return logging.getLogger("llm-serving-stack")


log = _setup()
