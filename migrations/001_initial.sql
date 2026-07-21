-- FigWatch initial schema: audit work queue + processed comment deduplication.

CREATE TABLE audit_queue (
    audit_id        TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'processing', 'completed', 'failed')),
    audit_payload   JSONB NOT NULL,
    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    attempt         INTEGER NOT NULL DEFAULT 1,
    ack_id          TEXT,
    ack_position    INTEGER,
    retry_after     TIMESTAMPTZ,
    trace_context   JSONB,
    locked_by       TEXT,
    locked_at       TIMESTAMPTZ,
    -- Denormalized for direct querying without digging into JSONB
    user_handle     TEXT,
    trigger_keyword TEXT,
    file_key        TEXT,
    trace_id        TEXT
);

-- Partial index for dequeue and ack updater: covers queued items ordered by enqueued_at.
-- The retry_after filter is applied at query time (now() is not IMMUTABLE).
CREATE INDEX idx_audit_queue_queued ON audit_queue (enqueued_at)
    WHERE status = 'queued';

-- Index for audit history queries by user.
CREATE INDEX idx_audit_queue_user ON audit_queue (user_handle, enqueued_at DESC);

-- Index for audit history queries by status.
CREATE INDEX idx_audit_queue_status_time ON audit_queue (status, enqueued_at DESC);

CREATE TABLE processed_comments (
    comment_id   TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_processed_comments_age ON processed_comments (processed_at);
