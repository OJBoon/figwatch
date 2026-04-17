"""Tests for figwatch.webhook_monitor — rotating reconciliation thread."""

import threading
import time

import pytest

from figwatch.webhook_monitor import WebhookMonitor, _parse_iso


# ── _parse_iso ────────────────────────────────────────────────────────


def test_parse_iso_zulu():
    ts = _parse_iso('2026-04-17T12:00:00.000Z')
    assert ts is not None
    assert ts > 0


def test_parse_iso_offset():
    ts = _parse_iso('2026-04-17T12:00:00+00:00')
    assert ts is not None


def test_parse_iso_garbage():
    assert _parse_iso('not-a-date') is None


def test_parse_iso_empty():
    assert _parse_iso('') is None


# ── Helpers ───────────────────────────────────────────────────────────


class _FigmaStub:
    """Records API calls and returns canned responses."""

    def __init__(self):
        self.calls = []
        self.responses = {}

    def get(self, path, pat, retries=1):
        self.calls.append(path)
        return self.responses.get(path)


def _make_monitor(figma_stub, received_events=None, received_lock=None,
                  stop_event=None, extra_file_keys=None, **env_overrides):
    """Create a WebhookMonitor with stubbed Figma API."""
    received_events = received_events if received_events is not None else {}
    received_lock = received_lock or threading.Lock()
    stop_event = stop_event or threading.Event()

    mon = WebhookMonitor(
        pat='test-pat',
        team_id='team-1',
        extra_file_keys=extra_file_keys or set(),
        received_events=received_events,
        received_lock=received_lock,
        stop_event=stop_event,
    )
    # Inject stub — bypass rate limiter by replacing _api_get
    mon._api_get = lambda path: figma_stub.get(path, 'test-pat')
    return mon


def _comment(comment_id, created_at):
    """Build a minimal Figma comment dict."""
    return {'id': comment_id, 'created_at': created_at}


def _iso(unix_ts):
    """Convert unix timestamp to ISO 8601 string."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%S.000Z'
    )


# ── File discovery ────────────────────────────────────────────────────


def test_discover_files_from_team(monkeypatch):
    stub = _FigmaStub()
    stub.responses['/v1/teams/team-1/projects'] = {
        'projects': [{'id': 'proj-1'}, {'id': 'proj-2'}],
    }
    stub.responses['/v1/projects/proj-1/files'] = {
        'files': [{'key': 'file-a'}, {'key': 'file-b'}],
    }
    stub.responses['/v1/projects/proj-2/files'] = {
        'files': [{'key': 'file-c'}],
    }

    mon = _make_monitor(stub)
    # Disable metrics to avoid side effects
    monkeypatch.setattr('figwatch.webhook_monitor.record_files_tracked', lambda *a: None)

    mon._refresh_files()

    assert set(mon._file_keys) == {'file-a', 'file-b', 'file-c'}
    assert len(mon._file_state) == 3


def test_discover_files_includes_extra_keys(monkeypatch):
    stub = _FigmaStub()
    stub.responses['/v1/teams/team-1/projects'] = {
        'projects': [{'id': 'proj-1'}],
    }
    stub.responses['/v1/projects/proj-1/files'] = {
        'files': [{'key': 'file-a'}],
    }

    mon = _make_monitor(stub, extra_file_keys={'file-x', 'file-y'})
    monkeypatch.setattr('figwatch.webhook_monitor.record_files_tracked', lambda *a: None)
    mon._refresh_files()

    assert 'file-a' in mon._file_keys
    assert 'file-x' in mon._file_keys
    assert 'file-y' in mon._file_keys


def test_discover_files_keeps_state_on_failure(monkeypatch):
    stub = _FigmaStub()
    # First refresh succeeds
    stub.responses['/v1/teams/team-1/projects'] = {
        'projects': [{'id': 'proj-1'}],
    }
    stub.responses['/v1/projects/proj-1/files'] = {
        'files': [{'key': 'file-a'}],
    }
    mon = _make_monitor(stub)
    monkeypatch.setattr('figwatch.webhook_monitor.record_files_tracked', lambda *a: None)
    mon._refresh_files()
    assert mon._file_keys == ['file-a']

    # Second refresh fails (API returns None)
    mon._last_file_refresh = 0  # force refresh
    stub.responses['/v1/teams/team-1/projects'] = None
    mon._refresh_files()

    # Should keep existing file list
    assert mon._file_keys == ['file-a']


def test_discover_removes_stale_files(monkeypatch):
    stub = _FigmaStub()
    stub.responses['/v1/teams/team-1/projects'] = {
        'projects': [{'id': 'proj-1'}],
    }
    stub.responses['/v1/projects/proj-1/files'] = {
        'files': [{'key': 'file-a'}, {'key': 'file-b'}],
    }
    mon = _make_monitor(stub)
    monkeypatch.setattr('figwatch.webhook_monitor.record_files_tracked', lambda *a: None)
    mon._refresh_files()
    assert len(mon._file_keys) == 2

    # file-b removed from team
    mon._last_file_refresh = 0
    stub.responses['/v1/projects/proj-1/files'] = {
        'files': [{'key': 'file-a'}],
    }
    mon._refresh_files()
    assert mon._file_keys == ['file-a']
    assert 'file-b' not in mon._file_state


# ── Rotation ──────────────────────────────────────────────────────────


def test_rotation_advances_index(monkeypatch):
    stub = _FigmaStub()
    monkeypatch.setattr('figwatch.webhook_monitor.record_files_tracked', lambda *a: None)
    monkeypatch.setattr('figwatch.webhook_monitor.record_reconciliation', lambda *a: None)

    mon = _make_monitor(stub)
    mon._file_keys = ['file-a', 'file-b', 'file-c']
    mon._file_state = {k: None for k in mon._file_keys}
    mon._rotation_index = 0

    # Each call to _check_next_file should return empty data but advance index
    stub.responses['/v1/files/file-a/comments'] = {'comments': []}
    stub.responses['/v1/files/file-b/comments'] = {'comments': []}
    stub.responses['/v1/files/file-c/comments'] = {'comments': []}

    mon._check_next_file()
    assert mon._rotation_index == 1

    mon._check_next_file()
    assert mon._rotation_index == 2

    mon._check_next_file()
    assert mon._rotation_index == 0  # wraps


def test_rotation_empty_file_list(monkeypatch):
    stub = _FigmaStub()
    mon = _make_monitor(stub)
    mon._file_keys = []
    # Should not raise
    mon._check_next_file()


# ── Missed webhook detection ─────────────────────────────────────────


def test_detects_missed_webhook(monkeypatch):
    stub = _FigmaStub()
    monkeypatch.setattr('figwatch.webhook_monitor.record_reconciliation', lambda *a: None)

    missed_records = []
    monkeypatch.setattr(
        'figwatch.webhook_monitor.record_webhook_missed',
        lambda fk, cid: missed_records.append((fk, cid)),
    )

    now = time.time()
    # Comment created 120s ago — well past grace period
    comment_time = now - 120

    received = {}
    mon = _make_monitor(stub, received_events=received)
    mon._file_keys = ['file-a']
    mon._file_state = {'file-a': comment_time - 1}  # last checked before comment
    mon._start_time = comment_time - 10
    mon._rotation_index = 0

    stub.responses['/v1/files/file-a/comments'] = {
        'comments': [_comment('c-1', _iso(comment_time))],
    }

    mon._check_next_file()

    assert len(missed_records) == 1
    assert missed_records[0] == ('file-a', 'c-1')


def test_does_not_flag_received_comment(monkeypatch):
    stub = _FigmaStub()
    monkeypatch.setattr('figwatch.webhook_monitor.record_reconciliation', lambda *a: None)

    missed_records = []
    monkeypatch.setattr(
        'figwatch.webhook_monitor.record_webhook_missed',
        lambda fk, cid: missed_records.append((fk, cid)),
    )

    now = time.time()
    comment_time = now - 120

    received = {'c-1': comment_time}  # already received via webhook
    mon = _make_monitor(stub, received_events=received)
    mon._file_keys = ['file-a']
    mon._file_state = {'file-a': comment_time - 1}
    mon._start_time = comment_time - 10
    mon._rotation_index = 0

    stub.responses['/v1/files/file-a/comments'] = {
        'comments': [_comment('c-1', _iso(comment_time))],
    }

    mon._check_next_file()

    assert len(missed_records) == 0


def test_grace_period_respected(monkeypatch):
    stub = _FigmaStub()
    monkeypatch.setattr('figwatch.webhook_monitor.record_reconciliation', lambda *a: None)

    missed_records = []
    monkeypatch.setattr(
        'figwatch.webhook_monitor.record_webhook_missed',
        lambda fk, cid: missed_records.append((fk, cid)),
    )

    now = time.time()
    # Comment created 10s ago — within default 60s grace period
    comment_time = now - 10

    mon = _make_monitor(stub)
    mon._file_keys = ['file-a']
    mon._file_state = {'file-a': comment_time - 1}
    mon._start_time = comment_time - 5
    mon._rotation_index = 0

    stub.responses['/v1/files/file-a/comments'] = {
        'comments': [_comment('c-1', _iso(comment_time))],
    }

    mon._check_next_file()

    # Should NOT be flagged — still within grace period
    assert len(missed_records) == 0


def test_skips_comments_before_last_check(monkeypatch):
    stub = _FigmaStub()
    monkeypatch.setattr('figwatch.webhook_monitor.record_reconciliation', lambda *a: None)

    missed_records = []
    monkeypatch.setattr(
        'figwatch.webhook_monitor.record_webhook_missed',
        lambda fk, cid: missed_records.append((fk, cid)),
    )

    now = time.time()
    old_comment_time = now - 3600  # 1 hour ago
    last_checked = now - 120       # last checked 2 min ago

    mon = _make_monitor(stub)
    mon._file_keys = ['file-a']
    mon._file_state = {'file-a': last_checked}
    mon._start_time = now - 7200
    mon._rotation_index = 0

    stub.responses['/v1/files/file-a/comments'] = {
        'comments': [_comment('c-old', _iso(old_comment_time))],
    }

    mon._check_next_file()

    # Old comment predates last check — should be skipped
    assert len(missed_records) == 0


def test_dedup_missed_alerts(monkeypatch):
    stub = _FigmaStub()
    monkeypatch.setattr('figwatch.webhook_monitor.record_reconciliation', lambda *a: None)

    missed_records = []
    monkeypatch.setattr(
        'figwatch.webhook_monitor.record_webhook_missed',
        lambda fk, cid: missed_records.append((fk, cid)),
    )

    now = time.time()
    comment_time = now - 120

    mon = _make_monitor(stub)
    mon._file_keys = ['file-a']
    mon._file_state = {'file-a': comment_time - 1}
    mon._start_time = comment_time - 10
    mon._rotation_index = 0

    stub.responses['/v1/files/file-a/comments'] = {
        'comments': [_comment('c-1', _iso(comment_time))],
    }

    # Check twice — second should not re-report
    mon._check_next_file()
    mon._rotation_index = 0
    mon._file_state['file-a'] = comment_time - 1  # reset to re-scan
    mon._check_next_file()

    assert len(missed_records) == 1  # only reported once


def test_first_check_uses_start_time(monkeypatch):
    """When file has never been checked, use service start time as baseline."""
    stub = _FigmaStub()
    monkeypatch.setattr('figwatch.webhook_monitor.record_reconciliation', lambda *a: None)

    missed_records = []
    monkeypatch.setattr(
        'figwatch.webhook_monitor.record_webhook_missed',
        lambda fk, cid: missed_records.append((fk, cid)),
    )

    now = time.time()
    start_time = now - 300  # service started 5 min ago
    # Comment created before service started
    old_comment = now - 600

    mon = _make_monitor(stub)
    mon._file_keys = ['file-a']
    mon._file_state = {'file-a': None}  # never checked
    mon._start_time = start_time
    mon._rotation_index = 0

    stub.responses['/v1/files/file-a/comments'] = {
        'comments': [_comment('c-old', _iso(old_comment))],
    }

    mon._check_next_file()

    # Comment predates start_time — should not be flagged
    assert len(missed_records) == 0


# ── Stale eviction ───────────────────────────────────────────────────


def test_evict_stale_received():
    stub = _FigmaStub()
    now = time.time()
    received = {
        'old': now - 7200,   # 2 hours ago
        'recent': now - 10,  # 10 seconds ago
    }
    lock = threading.Lock()
    mon = _make_monitor(stub, received_events=received, received_lock=lock)
    mon._file_keys = ['file-a']
    mon._tick_interval = 60
    mon._grace_period = 60

    mon._evict_stale_received()

    assert 'old' not in received
    assert 'recent' in received


def test_evict_caps_reported_misses():
    stub = _FigmaStub()
    mon = _make_monitor(stub)
    mon._max_reported = 5
    mon._reported_misses = {f'c-{i}' for i in range(10)}

    mon._evict_stale_received()

    assert len(mon._reported_misses) == 0  # cleared when over cap


# ── Integration: threaded run ─────────────────────────────────────────


def test_monitor_thread_starts_and_stops(monkeypatch):
    monkeypatch.setattr('figwatch.webhook_monitor.record_files_tracked', lambda *a: None)
    monkeypatch.setattr('figwatch.webhook_monitor.record_reconciliation', lambda *a: None)

    stub = _FigmaStub()
    stub.responses['/v1/teams/team-1/projects'] = {'projects': []}

    stop = threading.Event()
    mon = _make_monitor(stub, stop_event=stop)
    mon._tick_interval = 0.05  # fast ticks for test

    mon.start()
    # Let it run a few ticks
    time.sleep(0.2)
    stop.set()
    mon.stop()

    assert not mon._thread.is_alive()
