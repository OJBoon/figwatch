"""Tests for figwatch.metrics — OTel metrics setup and recording helpers."""

import figwatch.metrics as m


# ── init_metrics noop when no endpoint ────────────────────────────────


def test_init_metrics_noop_without_endpoint(monkeypatch):
    """Metrics init is safe when OTEL_EXPORTER_OTLP_ENDPOINT is not set."""
    monkeypatch.delenv('OTEL_EXPORTER_OTLP_ENDPOINT', raising=False)
    # Reset module state
    m._meter = None
    m._webhook_received = None
    m._webhook_missed = None

    m.init_metrics()

    assert m._meter is None
    assert m._webhook_received is None


def test_init_metrics_noop_with_empty_endpoint(monkeypatch):
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', '  ')
    m._meter = None
    m.init_metrics()
    assert m._meter is None


# ── Recording helpers safe when uninitialised ─────────────────────────


def test_record_webhook_received_noop():
    """Recording helpers must not raise when instruments are None."""
    m._webhook_received = None
    m._webhook_last_received = None
    m.record_webhook_received('FILE_COMMENT')  # should not raise


def test_record_webhook_missed_noop():
    m._webhook_missed = None
    m.record_webhook_missed('abc123', 'comment-1')


def test_record_reconciliation_noop():
    m._monitor_reconciliation = None
    m._monitor_comments_checked = None
    m.record_reconciliation(5)


def test_record_files_tracked_noop():
    m._monitor_files_tracked = None
    m._monitor_rotation_seconds = None
    m.record_files_tracked(10, 60)


def test_record_audit_completed_noop():
    m._audit_duration = None
    m._audit_total = None
    m.record_audit_completed(12.5, 'success')


def test_record_queue_change_noop():
    m._queue_depth = None
    m.record_queue_change(1)
    m.record_queue_change(-1)


# ── Recording helpers call instruments when initialised ───────────────


class _FakeCounter:
    def __init__(self):
        self.calls = []

    def add(self, value, attributes=None):
        self.calls.append((value, attributes))


class _FakeGauge:
    def __init__(self):
        self.calls = []

    def set(self, value, attributes=None):
        self.calls.append((value, attributes))


class _FakeHistogram:
    def __init__(self):
        self.calls = []

    def record(self, value, attributes=None):
        self.calls.append((value, attributes))


def test_record_webhook_received_calls_instruments():
    counter = _FakeCounter()
    gauge = _FakeGauge()
    m._webhook_received = counter
    m._webhook_last_received = gauge

    m.record_webhook_received('PING')

    assert len(counter.calls) == 1
    assert counter.calls[0] == (1, {'event_type': 'PING'})
    assert len(gauge.calls) == 1
    # Gauge set to unix timestamp — just verify it's a positive number
    assert gauge.calls[0][0] > 0


def test_record_webhook_missed_calls_counter():
    counter = _FakeCounter()
    m._webhook_missed = counter

    m.record_webhook_missed('file-abc', 'comment-42')

    assert len(counter.calls) == 1
    assert counter.calls[0] == (1, {'file_key': 'file-abc'})


def test_record_audit_completed_calls_instruments():
    hist = _FakeHistogram()
    counter = _FakeCounter()
    m._audit_duration = hist
    m._audit_total = counter

    m.record_audit_completed(5.5, 'failed')

    assert hist.calls == [(5.5, None)]
    assert counter.calls == [(1, {'status': 'failed'})]


def test_record_queue_change_calls_updown():
    counter = _FakeCounter()  # UpDownCounter has same add() interface
    m._queue_depth = counter

    m.record_queue_change(1)
    m.record_queue_change(-1)

    assert counter.calls == [(1, None), (-1, None)]


def test_record_files_tracked_calls_gauges():
    files_gauge = _FakeGauge()
    rotation_gauge = _FakeGauge()
    m._monitor_files_tracked = files_gauge
    m._monitor_rotation_seconds = rotation_gauge

    m.record_files_tracked(50, 60)

    assert files_gauge.calls == [(50, None)]
    assert rotation_gauge.calls == [(3000, None)]
