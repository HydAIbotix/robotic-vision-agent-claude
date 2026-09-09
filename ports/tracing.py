"""
Tracing port — vendor-neutral observability (the CloudWatch-GenAI-traces analogue).

  none     (default) — no-op; zero overhead, byte-identical to the MVP.
  otel     — OpenTelemetry spans exported to any OTLP collector (Jaeger, Grafana Tempo, …).
  langfuse — LLM-native traces via LangChain's Langfuse callback handler.

Two entry points, both no-ops when tracing_backend=none:
  span(name, **attrs)  — a context manager wrapping a unit of work in a trace span (OTel).
  llm_callbacks()      — LangChain callbacks to pass at invoke time so LLM calls are traced
                         (used centrally in vision_agent/llm.invoke_json).

All SDKs are imported lazily, so nothing is added until a backend is selected. Init is idempotent and
failure-tolerant: a misconfigured exporter degrades to no-op rather than breaking a run.
"""
from __future__ import annotations

import contextlib
from typing import Iterator

from vision_agent.config import settings

_otel_tracer = None
_langfuse_handler = None
_init_done = False


def _init() -> None:
    global _otel_tracer, _langfuse_handler, _init_done
    if _init_done:
        return
    _init_done = True
    backend = settings.tracing_backend
    try:
        if backend == "otel":
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name}))
            if settings.otel_exporter_endpoint:
                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint)))
            trace.set_tracer_provider(provider)
            _otel_tracer = trace.get_tracer(settings.otel_service_name)
        elif backend == "langfuse":
            from langfuse.callback import CallbackHandler
            _langfuse_handler = CallbackHandler(
                public_key=settings.langfuse_public_key or None,
                secret_key=settings.langfuse_secret_key or None,
                host=settings.langfuse_host or None,
            )
    except Exception as exc:  # pragma: no cover - environment-dependent
        print(f"  [tracing] {backend} init failed ({exc}); continuing without tracing")


@contextlib.contextmanager
def span(name: str, **attrs) -> Iterator[None]:
    """Context manager tracing a unit of work. No-op unless the OTel backend is active."""
    if settings.tracing_backend == "none":
        yield
        return
    _init()
    if _otel_tracer is None:
        yield
        return
    with _otel_tracer.start_as_current_span(name) as sp:
        try:
            for k, v in attrs.items():
                sp.set_attribute(k, v)
        except Exception:
            pass
        yield


def llm_callbacks() -> list:
    """LangChain callbacks to attach to an LLM invoke (config={"callbacks": ...}). Empty unless the
    Langfuse backend is active, so passing this list is always safe and a no-op by default."""
    if settings.tracing_backend != "langfuse":
        return []
    _init()
    return [_langfuse_handler] if _langfuse_handler is not None else []


def reset() -> None:
    """Test hook — force re-init on next use after changing config."""
    global _otel_tracer, _langfuse_handler, _init_done
    _otel_tracer = None
    _langfuse_handler = None
    _init_done = False
