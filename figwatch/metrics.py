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
_webhook_missed = None
_webhook_last_received = None
_monitor_reconciliation = None
_monitor_comments_checked = None
_monitor_files_tracked = None
_monitor_rotation_seconds = None
_audit_duration = None
_audit_total = None
_queue_depth = None


def init_metrics(service_name='figwatch'):
    """Initialise OTel metrics. Safe to call unconditionally — noops if
    OTEL_EXPORTER_OTLP_ENDPOINT is not set.
    """
    global _meter
    global _webhook_received, _webhook_missed, _webhook_last_received
    global _monitor_reconciliation, _monitor_comments_checked
    global _monitor_files_tracked, _monitor_rotation_seconds
    global _audit_duration, _audit_total, _queue_depth

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
            View,
            ExplicitBucketHistogramAggregation,
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
    _webhook_missed = _meter.create_counter(
        'figwatch.webhook.missed_total',
        description='Comments found in Figma but never received via webhook',
    )
    _webhook_last_received = _meter.create_gauge(
        'figwatch.webhook.last_received_seconds',
        description='Unix timestamp of last webhook event',
    )

    # Monitor reconciliation
    _monitor_reconciliation = _meter.create_counter(
        'figwatch.monitor.reconciliation_total',
        description='Files checked (reconciliation ticks)',
    )
    _monitor_comments_checked = _meter.create_counter(
        'figwatch.monitor.comments_checked',
        description='Total comments evaluated across all ticks',
    )
    _monitor_files_tracked = _meter.create_gauge(
        'figwatch.monitor.files_tracked',
        description='Number of files in monitoring rotation',
    )
    _monitor_rotation_seconds = _meter.create_gauge(
        'figwatch.monitor.rotation_seconds',
        description='Estimated seconds for one full rotation',
    )

    # Audit processing
    _audit_duration = _meter.create_histogram(
        'figwatch.audit.duration_seconds',
        description='End-to-end audit time (queue wait + processing)',
        unit='s',
    )
    _audit_total = _meter.create_counter(
        'figwatch.audit.total',
        description='Audits completed',
    )
    _queue_depth = _meter.create_up_down_counter(
        'figwatch.queue.depth',
        description='Current queue depth',
    )

    logger.info('OTel metrics initialised', extra={'endpoint': endpoint})


# ── Recording helpers ────────────────────────────────────────────────


def record_webhook_received(event_type):
    if _webhook_received:
        _webhook_received.add(1, {'event_type': event_type})
    if _webhook_last_received:
        _webhook_last_received.set(time.time())


def record_webhook_missed(file_key, comment_id):
    if _webhook_missed:
        _webhook_missed.add(1, {'file_key': file_key})


def record_reconciliation(comments_checked):
    if _monitor_reconciliation:
        _monitor_reconciliation.add(1)
    if _monitor_comments_checked:
        _monitor_comments_checked.add(comments_checked)


def record_files_tracked(count, tick_interval):
    if _monitor_files_tracked:
        _monitor_files_tracked.set(count)
    if _monitor_rotation_seconds:
        _monitor_rotation_seconds.set(count * tick_interval)


def record_audit_completed(duration_seconds, status):
    if _audit_duration:
        _audit_duration.record(duration_seconds)
    if _audit_total:
        _audit_total.add(1, {'status': status})


def record_queue_change(delta):
    """Call with +1 on enqueue, -1 on dequeue."""
    if _queue_depth:
        _queue_depth.add(delta)
