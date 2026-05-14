"""OpenTelemetry metrics for FigWatch webhook monitoring.

Initialises a MeterProvider with OTLP gRPC exporter when
OTEL_EXPORTER_OTLP_ENDPOINT is set. Falls back to noop (zero overhead)
when the endpoint is not configured.
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

# Lazy-initialised instruments — populated by init_metrics().
_meter = None
_webhook_received = None
_webhook_last_received = None
_audit_duration = None
_queue_depth_source = None
_token_expired = None


def init_metrics(service_name='figwatch'):
    """Initialise OTel metrics. Safe to call unconditionally — noops if
    OTEL_EXPORTER_OTLP_ENDPOINT is not set.
    """
    global _meter
    global _webhook_received, _webhook_last_received
    global _audit_duration, _queue_depth_source
    global _token_expired

    endpoint = os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT', '').strip()
    if not endpoint:
        logger.info('OTEL_EXPORTER_OTLP_ENDPOINT not set — metrics disabled')
        return

    try:
        from opentelemetry import metrics
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.metrics.view import (
            ExplicitBucketHistogramAggregation,
            View,
        )
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        logger.warning(
            'opentelemetry packages not installed — metrics disabled. '
            'Install with: pip install "figwatch[server]"'
        )
        return

    resource = Resource.create({'service.name': service_name})
    reader = PeriodicExportingMetricReader(OTLPMetricExporter())

    # Custom buckets for audit durations — default OTel buckets are tuned for
    # sub-second HTTP latencies, but AI audits typically take 5-120 seconds.
    audit_duration_view = View(
        instrument_name='figwatch.audit.duration_seconds',
        aggregation=ExplicitBucketHistogramAggregation(
            boundaries=[1, 2, 5, 10, 15, 30, 45, 60, 90, 120, 180, 300, 450, 600, 900],
        ),
    )

    provider = MeterProvider(
        resource=resource,
        metric_readers=[reader],
        views=[audit_duration_view],
    )
    metrics.set_meter_provider(provider)

    _meter = provider.get_meter('figwatch')

    # Webhook delivery tracking
    _webhook_received = _meter.create_counter(
        'figwatch.webhook.received_total',
        description='Webhook events received',
    )
    _webhook_last_received = _meter.create_gauge(
        'figwatch.webhook.last_received_seconds',
        description='Unix timestamp of last webhook event',
    )

    # Audit processing
    _audit_duration = _meter.create_histogram(
        'figwatch.audit.duration_seconds',
        description='End-to-end audit time (queue wait + processing)',
        unit='s',
    )

    def _observe_queue_depth(options):
        if _queue_depth_source is not None:
            yield metrics.Observation(value=_queue_depth_source())

    _meter.create_observable_gauge(
        'figwatch.queue.depth',
        callbacks=[_observe_queue_depth],
        description='Current queue depth',
    )

    _token_expired = _meter.create_counter(
        'figwatch.auth.token_expired',
        description='Figma token expiry events detected',
    )

    logger.info('OTel metrics initialised', extra={'endpoint': endpoint})


# ── Recording helpers ────────────────────────────────────────────────


def record_webhook_received(event_type):
    if _webhook_received:
        _webhook_received.add(1, {'event_type': event_type})
    if _webhook_last_received:
        _webhook_last_received.set(time.time())


def record_audit_completed(duration_seconds, status, user_handle=None):
    if _audit_duration:
        attrs = {'status': status}
        if user_handle:
            attrs['figma.user'] = user_handle
        _audit_duration.record(duration_seconds, attrs)


def record_token_expired():
    if _token_expired:
        _token_expired.add(1)


def set_queue_depth_source(fn):
    """Register a callable that returns current queue depth.

    Called by the OTel SDK on each metrics export — no manual +1/-1 needed.
    """
    global _queue_depth_source
    _queue_depth_source = fn
