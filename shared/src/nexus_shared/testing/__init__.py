"""Test helpers shared across services (no pytest dependency at import time)."""

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

__all__ = ["InMemorySpanExporter"]
