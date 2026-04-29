"""OpenTelemetry tracing for FigWatch audit lifecycle.

Initialises a TracerProvider with OTLP gRPC exporter when
OTEL_EXPORTER_OTLP_ENDPOINT is set. Falls back to noop (zero overhead)
when the endpoint is not configured.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_tracer = None


def init_tracing(service_name='figwatch'):
    """Initialise OTel tracing. Safe to call unconditionally — noops if
    OTEL_EXPORTER_OTLP_ENDPOINT is not set.
    """
    global _tracer

    endpoint = os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT', '').strip()
    if not endpoint:
        logger.info('OTEL_EXPORTER_OTLP_ENDPOINT not set — tracing disabled')
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            'opentelemetry packages not installed — tracing disabled. '
            'Install with: pip install "figwatch[server]"'
        )
        return

    resource = Resource.create({'service.name': service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    _tracer = trace.get_tracer('figwatch')
    logger.info('OTel tracing initialised', extra={'endpoint': endpoint})


def get_tracer():
    """Return the initialised tracer, or a noop tracer if tracing is disabled."""
    if _tracer:
        return _tracer
    try:
        from opentelemetry import trace
        return trace.get_tracer('figwatch')
    except ImportError:
        return _NoopTracer()


def get_trace_id(otel_context=None) -> str:
    """Return the OTel trace ID as a hex string, or '' if unavailable.

    If *otel_context* is given, extract the trace ID from that context
    (useful when the caller runs in a different thread). Otherwise read
    from the current span.
    """
    try:
        from opentelemetry import trace
        if otel_context is not None:
            from opentelemetry import context
            token = context.attach(otel_context)
            try:
                span = trace.get_current_span()
            finally:
                context.detach(token)
        else:
            span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, '032x')
    except (ImportError, AttributeError):
        pass
    return ''


class _NoopSpan:
    """Minimal stand-in when opentelemetry is not installed."""

    def set_attribute(self, key, value):
        pass

    def set_status(self, *args, **kwargs):
        pass

    def record_exception(self, exception):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _NoopTracer:
    """Minimal stand-in when opentelemetry is not installed."""

    def start_as_current_span(self, name, **kwargs):
        return _NoopSpan()


class TracedThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor that propagates OTel context to worker threads."""

    def submit(self, fn, /, *args, **kwargs):
        try:
            from opentelemetry import context
            ctx = context.get_current()

            def _wrapped():
                token = context.attach(ctx)
                try:
                    return fn(*args, **kwargs)
                finally:
                    context.detach(token)

            return super().submit(_wrapped)
        except ImportError:
            return super().submit(fn, *args, **kwargs)
