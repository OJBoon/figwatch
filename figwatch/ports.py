"""Repository protocols — ports for infrastructure access.

Structural (duck-typed) protocols. Implementations live in providers/.
"""

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class CommentRepository(Protocol):
    """Port for reading and writing Figma comments."""

    def post_reply(self, file_key: str, parent_comment_id: str, message: str) -> Optional[str]:
        """Post a comment reply. Returns comment ID. Raises on failure."""
        ...

    def delete_comment(self, file_key: str, comment_id: str) -> None:
        """Delete a comment. Silent on failure."""
        ...

    def comment_exists(self, file_key: str, comment_id: str) -> bool:
        """Check whether a comment still exists on a file."""
        ...

    def fetch_comments(self, file_key: str) -> list:
        """Fetch all comments for a file."""
        ...


@runtime_checkable
class DesignDataRepository(Protocol):
    """Port for fetching Figma design data needed by skills."""

    def fetch(
        self, required_data: list, file_key: str, node_id: str,
    ) -> tuple[dict, Optional[dict]]:
        """Fetch design data. Returns (data_dict, tree_data_or_None)."""
        ...


# ── Queue DTOs ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class QueueRow:
    """Data returned by AuditQueueRepository.dequeue()."""
    audit_id: str
    audit: Any            # deserialized Audit aggregate
    ack_id: Optional[str]
    attempt: int
    enqueued_at: float    # Unix timestamp
    trace_context: Optional[dict]


@dataclass(frozen=True)
class AckUpdateRow:
    """Data returned by AuditQueueRepository.pending_ack_updates()."""
    audit_id: str
    audit: Any            # deserialized Audit aggregate
    ack_id: Optional[str]
    current_position: int
    displayed_position: Optional[int]


# ── Audit queue port ──────────────────────────────────────────────


@runtime_checkable
class AuditQueueRepository(Protocol):
    """Port for the durable work queue backed by PostgreSQL."""

    def enqueue(self, audit: Any, ack_id: Optional[str],
                trace_context: Optional[dict]) -> int:
        """Insert an audit into the queue. Returns queue position (items ahead)."""
        ...

    def dequeue(self, worker_id: str, timeout: float = 30.0) -> Optional[QueueRow]:
        """Claim the next eligible row. Blocks up to timeout seconds.
        Returns None if nothing available."""
        ...

    def complete(self, audit_id: str) -> None:
        """Mark an audit as completed."""
        ...

    def fail(self, audit_id: str, retry_after_seconds: Optional[int] = None) -> None:
        """Mark failed. If retry_after_seconds given, schedule retry."""
        ...

    def update_ack(self, audit_id: str, ack_id: str, position: int) -> None:
        """Update the ack_id and displayed position for a queued audit."""
        ...

    def pending_ack_updates(self) -> list:
        """Return queued audits whose position differs from ack_position."""
        ...

    def queue_depth(self) -> int:
        """Return count of queued rows."""
        ...

    def is_comment_processed(self, comment_id: str) -> bool:
        """Check if a comment ID has been processed."""
        ...

    def mark_comment_processed(self, comment_id: str) -> bool:
        """Insert comment ID. Returns False if already exists (duplicate)."""
        ...

    def cleanup_old_comments(self, max_age_days: int = 7) -> int:
        """Delete processed_comments older than max_age_days."""
        ...

    def cleanup_old_audits(self, max_age_days: int = 7) -> int:
        """Delete completed/failed audits older than max_age_days."""
        ...

    def check_health(self) -> None:
        """Verify connectivity and schema existence."""
        ...
