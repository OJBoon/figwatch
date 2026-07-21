"""PostgreSQL-backed audit queue repository.

Replaces the in-memory InstrumentedQueue, BoundedSet, and AckUpdater state
with durable PostgreSQL tables. Uses psycopg v3 with connection pooling and
LISTEN/NOTIFY for instant worker wakeup.
"""

import glob
import logging
import os
import re
import time
from dataclasses import asdict
from typing import Optional

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from figwatch.domain import Audit, Comment, Trigger, TriggerMatch
from figwatch.ports import AckUpdateRow, QueueRow
from figwatch.tracing import get_trace_id

logger = logging.getLogger(__name__)


# ── Audit serialization ──────────────────────────────────────────


def _serialize_audit(audit: Audit) -> dict:
    """Convert an Audit aggregate to a JSON-serializable dict."""
    return {
        'audit_id': audit.audit_id,
        'status': audit.status.value,
        'comment': asdict(audit.comment),
        'trigger_match': {
            'trigger': asdict(audit.trigger_match.trigger),
            'extra': audit.trigger_match.extra,
        },
    }


def _deserialize_audit(data: dict) -> Audit:
    """Reconstruct an Audit aggregate from a JSON dict."""
    from figwatch.domain import AuditStatus
    return Audit(
        audit_id=data['audit_id'],
        comment=Comment(**data['comment']),
        trigger_match=TriggerMatch(
            trigger=Trigger(**data['trigger_match']['trigger']),
            extra=data['trigger_match']['extra'],
        ),
        status=AuditStatus(data['status']),
    )


# ── Migration runner ─────────────────────────────────────────────


def run_migrations(conn, migrations_dir: str) -> int:
    """Apply unapplied versioned SQL migrations. Returns count applied."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version  INTEGER PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("SELECT version FROM schema_version ORDER BY version")
        applied = {row['version'] for row in cur.fetchall()}

    pattern = os.path.join(migrations_dir, '*.sql')
    files = sorted(glob.glob(pattern))
    version_re = re.compile(r'^(\d+)')

    count = 0
    for path in files:
        match = version_re.match(os.path.basename(path))
        if not match:
            continue
        version = int(match.group(1))
        if version in applied:
            continue

        logger.info('applying migration',
                    extra={'version': version, 'file': os.path.basename(path)})
        with open(path) as f:
            migration_sql = f.read()
        with conn.cursor() as cur:
            cur.execute(sql.SQL(migration_sql))
            cur.execute(
                "INSERT INTO schema_version (version) VALUES (%s)",
                (version,),
            )
        count += 1

    conn.commit()
    return count


# ── Repository implementation ────────────────────────────────────


class PgAuditQueueRepository:
    """PostgreSQL implementation of AuditQueueRepository."""

    def __init__(self, database_url: str, pool_size: int = 6,
                 migrations_dir: Optional[str] = None):
        self._pool = ConnectionPool(
            database_url,
            min_size=1,
            max_size=pool_size,
            kwargs={'row_factory': dict_row, 'autocommit': True},
        )
        if migrations_dir:
            with self._pool.connection() as conn:
                # Migrations need explicit transaction control.
                conn.autocommit = False
                applied = run_migrations(conn, migrations_dir)
                if applied:
                    logger.info('migrations applied', extra={'count': applied})
                conn.autocommit = True

    def close(self) -> None:
        self._pool.close()

    # ── Queue operations ──────────────────────────────────────────

    def enqueue(self, audit: Audit, ack_id: Optional[str]) -> int:
        with self._pool.connection() as conn:
            trace_id = get_trace_id() or None
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO audit_queue
                        (audit_id, audit_payload, ack_id,
                         user_handle, trigger_keyword, file_key, trace_id)
                    VALUES (%(audit_id)s, %(payload)s, %(ack_id)s,
                            %(user_handle)s, %(trigger)s, %(file_key)s,
                            %(trace_id)s)
                """, {
                    'audit_id': audit.audit_id,
                    'payload': Jsonb(_serialize_audit(audit)),
                    'ack_id': ack_id,
                    'user_handle': audit.comment.user_handle,
                    'trigger': audit.trigger_match.trigger.keyword,
                    'file_key': audit.comment.file_key,
                    'trace_id': trace_id,
                })

                cur.execute("""
                    SELECT count(*) AS ahead FROM audit_queue
                    WHERE status = 'queued'
                      AND enqueued_at < (
                          SELECT enqueued_at FROM audit_queue
                          WHERE audit_id = %(audit_id)s
                      )
                """, {'audit_id': audit.audit_id})
                position = cur.fetchone()['ahead']

        return position

    def dequeue(self, worker_id: str, timeout: float = 30.0) -> Optional[QueueRow]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._pool.connection() as conn:
                row = self._try_dequeue(conn, worker_id)
                if row:
                    return row
            # Poll interval — short enough for responsive dequeue,
            # long enough to avoid hammering the database.
            time.sleep(1.0)
        return None

    def _try_dequeue(self, conn, worker_id: str) -> Optional[QueueRow]:
        with conn.cursor() as cur:
            cur.execute("""
                WITH next AS (
                    SELECT audit_id FROM audit_queue
                    WHERE status = 'queued'
                      AND (retry_after IS NULL OR retry_after <= now())
                    ORDER BY enqueued_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE audit_queue
                SET status = 'processing',
                    locked_by = %(worker_id)s,
                    locked_at = now()
                FROM next
                WHERE audit_queue.audit_id = next.audit_id
                RETURNING audit_queue.*
            """, {'worker_id': worker_id})
            row = cur.fetchone()

        if row is None:
            return None

        return QueueRow(
            audit_id=row['audit_id'],
            audit=_deserialize_audit(row['audit_payload']),
            ack_id=row['ack_id'],
            attempt=row['attempt'],
            enqueued_at=row['enqueued_at'].timestamp(),
        )

    def complete(self, audit_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("""
                UPDATE audit_queue
                SET status = 'completed', completed_at = now()
                WHERE audit_id = %s
            """, (audit_id,))

    def fail(self, audit_id: str, retry_after_seconds: Optional[int] = None) -> None:
        with self._pool.connection() as conn:
            if retry_after_seconds is not None:
                conn.execute("""
                    UPDATE audit_queue
                    SET status = 'queued',
                        attempt = attempt + 1,
                        retry_after = now() + make_interval(secs => %(secs)s),
                        locked_by = NULL,
                        locked_at = NULL
                    WHERE audit_id = %(id)s
                """, {'id': audit_id, 'secs': retry_after_seconds})
            else:
                conn.execute("""
                    UPDATE audit_queue
                    SET status = 'failed', completed_at = now()
                    WHERE audit_id = %s
                """, (audit_id,))

    # ── Ack tracking ──────────────────────────────────────────────

    def update_ack(self, audit_id: str, ack_id: str, position: int) -> None:
        with self._pool.connection() as conn:
            conn.execute("""
                UPDATE audit_queue
                SET ack_id = %s, ack_position = %s
                WHERE audit_id = %s
            """, (ack_id, position, audit_id))

    def pending_ack_updates(self) -> list:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                    audit_id, audit_payload, ack_id, ack_position,
                    (ROW_NUMBER() OVER (ORDER BY enqueued_at) - 1) AS current_position
                FROM audit_queue
                WHERE status = 'queued'
                ORDER BY enqueued_at
            """)
            rows = cur.fetchall()

        result = []
        for row in rows:
            current = row['current_position']
            displayed = row['ack_position']
            if displayed is not None and current == displayed:
                continue
            result.append(AckUpdateRow(
                audit_id=row['audit_id'],
                audit=_deserialize_audit(row['audit_payload']),
                ack_id=row['ack_id'],
                current_position=current,
                displayed_position=displayed,
            ))
        return result

    def queue_depth(self) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS depth FROM audit_queue WHERE status = 'queued'")
            return cur.fetchone()['depth']

    # ── Processed comment deduplication ───────────────────────────

    def is_comment_processed(self, comment_id: str) -> bool:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM processed_comments WHERE comment_id = %s",
                (comment_id,),
            )
            return cur.fetchone() is not None

    def mark_comment_processed(self, comment_id: str) -> bool:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO processed_comments (comment_id)
                VALUES (%s)
                ON CONFLICT (comment_id) DO NOTHING
                RETURNING comment_id
            """, (comment_id,))
            return cur.fetchone() is not None

    # ── Audit history queries ────────────────────────────────────

    def query_audits(self, status=None, user_handle=None, limit=50):
        """Query audit history. Returns list of dicts."""
        conditions = []
        params = {}
        if status:
            conditions.append("status = %(status)s")
            params['status'] = status
        if user_handle:
            conditions.append("user_handle = %(user_handle)s")
            params['user_handle'] = user_handle

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params['limit'] = limit

        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"""
                SELECT audit_id, status, user_handle, trigger_keyword,
                       file_key, enqueued_at, completed_at, attempt,
                       trace_id
                FROM audit_queue
                {where}
                ORDER BY enqueued_at DESC
                LIMIT %(limit)s
            """, params)
            return cur.fetchall()

    # ── Cleanup ───────────────────────────────────────────────────

    def cleanup_old_comments(self, max_age_days: int = 7) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""
                DELETE FROM processed_comments
                WHERE processed_at < now() - make_interval(days => %s)
            """, (max_age_days,))
            return cur.rowcount

    def cleanup_old_audits(self, max_age_days: int = 7) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""
                DELETE FROM audit_queue
                WHERE status IN ('completed', 'failed')
                  AND enqueued_at < now() - make_interval(days => %s)
            """, (max_age_days,))
            return cur.rowcount

    # ── Health check ──────────────────────────────────────────────

    def check_health(self) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            for table in ('audit_queue', 'processed_comments', 'schema_version'):
                cur.execute("""
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = %s
                """, (table,))
                if cur.fetchone() is None:
                    raise RuntimeError(
                        f'table {table!r} does not exist — run migrations'
                    )
