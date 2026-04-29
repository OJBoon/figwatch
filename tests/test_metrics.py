"""Tests for figwatch.metrics — OTel metrics setup and recording helpers."""

import figwatch.metrics as m

# ── init_metrics noop when no endpoint ────────────────────────────────


def test_init_metrics_noop_without_endpoint(monkeypatch):
    """Metrics init is safe when OTEL_EXPORTER_OTLP_ENDPOINT is not set."""
    monkeypatch.delenv('OTEL_EXPORTER_OTLP_ENDPOINT', raising=False)
    # Reset module state
    m._meter = None
    m._webhook_received = None

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



def test_record_audit_completed_noop():
    m._audit_duration = None
    m.record_audit_completed(12.5, 'success')


def test_record_token_expired_noop():
    m._token_expired = None
    m.record_token_expired()  # should not raise


def test_set_queue_depth_source():
    m._queue_depth_source = None
    m.set_queue_depth_source(lambda: 42)
    assert m._queue_depth_source() == 42
    m._queue_depth_source = None  # cleanup


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



def test_record_audit_completed_calls_instruments():
    hist = _FakeHistogram()
    m._audit_duration = hist

    m.record_audit_completed(5.5, 'failed')

    assert hist.calls == [(5.5, {'status': 'failed'})]


def test_record_audit_completed_includes_user_handle():
    hist = _FakeHistogram()
    m._audit_duration = hist

    m.record_audit_completed(3.0, 'success', user_handle='alice')

    assert hist.calls == [(3.0, {'status': 'success', 'figma.user': 'alice'})]


def test_set_queue_depth_source_registers_callable():
    depth = [0]
    m.set_queue_depth_source(lambda: depth[0])
    assert m._queue_depth_source() == 0
    depth[0] = 3
    assert m._queue_depth_source() == 3
    m._queue_depth_source = None  # cleanup


