"""Webhook health monitor — detects missed Figma webhooks via comment reconciliation.

Discovers all files in a Figma team, then rotates through them one per tick,
fetching recent comments and comparing against webhook-received IDs. Comments
that exist in Figma but were never delivered via webhook are flagged as missed.

Rate-limited independently so monitor traffic never starves audit API calls.
"""

import logging
import os
import threading
import time
import urllib.error
from datetime import datetime, timezone

from figwatch.metrics import (
    record_files_tracked,
    record_reconciliation,
    record_webhook_missed,
)
from figwatch.providers.ai.rate_limit import TokenBucket
from figwatch.providers.figma import figma_get_retry

logger = logging.getLogger(__name__)

# Monitor gets a conservative slice of Figma's rate budget.
# Default: 5 req/min — leaves ~25 req/min for audit operations.
_DEFAULT_MONITOR_RPM = 5


def _parse_iso(s):
    """Parse ISO 8601 timestamp from Figma API to unix seconds."""
    # Figma returns e.g. "2026-04-17T12:34:56.000Z"
    s = s.replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


class WebhookMonitor:
    """Background thread that detects missed Figma webhooks.

    Discovers team files on startup (+ periodic refresh), then checks one
    file per tick in round-robin order. For each file, fetches comments
    created since the last check and verifies they were received via webhook.

    All Figma API calls go through a dedicated TokenBucket rate limiter
    (default 5 req/min) so monitor traffic doesn't compete with audit
    operations for the shared PAT rate budget.

    Args:
        pat: Figma Personal Access Token.
        team_id: Figma team ID for file discovery.
        extra_file_keys: Additional file keys to monitor (from FIGWATCH_FILES).
        received_events: Shared dict {comment_id: receive_unix_timestamp}.
        received_lock: Lock protecting received_events.
        stop_event: Threading event signalling shutdown.
    """

    def __init__(self, pat, team_id, extra_file_keys, received_events,
                 received_lock, stop_event):
        self._pat = pat
        self._team_id = team_id
        self._extra_file_keys = extra_file_keys or set()
        self._received_events = received_events
        self._received_lock = received_lock
        self._stop = stop_event

        self._tick_interval = int(
            os.environ.get('FIGWATCH_MONITOR_TICK', '60')
        )
        self._grace_period = int(
            os.environ.get('FIGWATCH_MONITOR_GRACE', '60')
        )
        self._file_refresh_interval = int(
            os.environ.get('FIGWATCH_MONITOR_FILE_REFRESH', '3600')
        )
        monitor_rpm = int(
            os.environ.get('FIGWATCH_MONITOR_RPM', str(_DEFAULT_MONITOR_RPM))
        )

        # Rate limiter — all monitor API calls acquire a token first.
        self._limiter = TokenBucket(
            capacity=monitor_rpm,
            refill_per_second=monitor_rpm / 60.0,
        )

        # Per-file state: {file_key: last_checked_at (unix timestamp)}
        self._file_state = {}
        # Ordered list of file keys for round-robin
        self._file_keys = []
        self._rotation_index = 0
        self._last_file_refresh = 0.0
        self._start_time = time.time()

        # Track already-reported missed IDs to avoid duplicate alerts
        self._reported_misses = set()
        # Cap reported misses set to prevent unbounded growth
        self._max_reported = 10000

        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name='figwatch-webhook-monitor', daemon=True,
        )
        self._thread.start()

    def stop(self):
        if self._thread:
            self._thread.join(timeout=5)

    # ── Rate-limited Figma API call ──────────────────────────────────

    def _api_get(self, path):
        """Figma GET with monitor rate limiting and extended 429 backoff.

        Acquires a token from the monitor's bucket before calling,
        and backs off longer on 429 to yield bandwidth to audits.
        """
        self._limiter.acquire()
        data = figma_get_retry(path, self._pat, retries=1)
        return data

    # ── File discovery ───────────────────────────────────────────────

    def _discover_files(self):
        """Fetch all file keys from team projects via Figma API."""
        file_keys = set()

        projects = self._api_get(f'/v1/teams/{self._team_id}/projects')
        if not projects or 'projects' not in projects:
            logger.warning(
                'monitor: failed to fetch team projects',
                extra={'team_id': self._team_id},
            )
            return None

        for project in projects['projects']:
            if self._stop.is_set():
                return None
            project_id = project.get('id')
            if not project_id:
                continue

            files = self._api_get(f'/v1/projects/{project_id}/files')
            if files and 'files' in files:
                for f in files['files']:
                    fk = f.get('key')
                    if fk:
                        file_keys.add(fk)

        return file_keys

    def _refresh_files(self):
        """Refresh the monitored file list if refresh interval elapsed."""
        now = time.time()
        if now - self._last_file_refresh < self._file_refresh_interval:
            return

        discovered = self._discover_files()
        if discovered is None:
            # Discovery failed or shutdown — keep existing list
            return

        all_keys = discovered | self._extra_file_keys

        # Merge: keep existing last_checked_at for known files
        for fk in all_keys:
            if fk not in self._file_state:
                self._file_state[fk] = None  # never checked

        # Remove files no longer in team (but keep extra_file_keys)
        stale = set(self._file_state) - all_keys
        for fk in stale:
            del self._file_state[fk]

        self._file_keys = sorted(self._file_state.keys())
        # Reset rotation if list changed significantly
        if self._rotation_index >= len(self._file_keys):
            self._rotation_index = 0

        self._last_file_refresh = now

        record_files_tracked(len(self._file_keys), self._tick_interval)
        logger.info(
            'monitor: file list refreshed',
            extra={
                'files': len(self._file_keys),
                'rotation_period': f'{len(self._file_keys) * self._tick_interval}s',
            },
        )

    # ── Reconciliation ───────────────────────────────────────────────

    def _check_next_file(self):
        """Check one file for missed webhooks, advance rotation index."""
        if not self._file_keys:
            return

        file_key = self._file_keys[self._rotation_index]
        self._rotation_index = (self._rotation_index + 1) % len(self._file_keys)

        since = self._file_state.get(file_key) or self._start_time
        now = time.time()

        data = self._api_get(f'/v1/files/{file_key}/comments')
        if not data or 'comments' not in data:
            logger.debug(
                'monitor: no comments data',
                extra={'file': file_key},
            )
            self._file_state[file_key] = now
            record_reconciliation(0)
            return

        comments = data['comments']
        checked = 0
        missed = 0

        for comment in comments:
            created_str = comment.get('created_at')
            if not created_str:
                continue
            created_at = _parse_iso(created_str)
            if created_at is None:
                continue

            # Only check comments created since last check
            if created_at < since:
                continue

            # Grace period — give webhook time to arrive
            if now - created_at < self._grace_period:
                continue

            comment_id = str(comment.get('id', ''))
            if not comment_id:
                continue

            checked += 1

            # Check if we received this via webhook
            with self._received_lock:
                received = comment_id in self._received_events

            if not received and comment_id not in self._reported_misses:
                missed += 1
                self._reported_misses.add(comment_id)
                record_webhook_missed(file_key, comment_id)
                logger.warning(
                    'monitor: missed webhook detected',
                    extra={
                        'file': file_key,
                        'comment_id': comment_id,
                        'comment_age_seconds': int(now - created_at),
                    },
                )

        self._file_state[file_key] = now
        record_reconciliation(checked)

        if checked > 0:
            logger.debug(
                'monitor: file checked',
                extra={
                    'file': file_key,
                    'comments_checked': checked,
                    'missed': missed,
                },
            )

    def _evict_stale_received(self):
        """Remove old entries from received_events to prevent unbounded growth."""
        # Keep entries for at least the full rotation period + grace
        max_age = max(
            len(self._file_keys) * self._tick_interval + self._grace_period,
            3600,  # minimum 1 hour
        )
        cutoff = time.time() - max_age
        with self._received_lock:
            stale = [k for k, v in self._received_events.items() if v < cutoff]
            for k in stale:
                del self._received_events[k]

        # Also cap reported misses
        if len(self._reported_misses) > self._max_reported:
            self._reported_misses.clear()

    # ── Main loop ────────────────────────────────────────────────────

    def _run(self):
        logger.info(
            'monitor: starting',
            extra={
                'team_id': self._team_id,
                'tick_interval': self._tick_interval,
                'grace_period': self._grace_period,
                'monitor_rpm': self._limiter._capacity,
            },
        )

        # Initial file discovery
        self._refresh_files()

        tick_count = 0
        while not self._stop.is_set():
            try:
                self._refresh_files()
                self._check_next_file()

                # Evict stale data every 100 ticks
                tick_count += 1
                if tick_count % 100 == 0:
                    self._evict_stale_received()

            except Exception:
                logger.exception('monitor: tick failed')

            self._stop.wait(timeout=self._tick_interval)

        logger.info('monitor: stopped')
