"""Application services — orchestration layer between domain and infrastructure."""

import logging
from dataclasses import dataclass
from typing import Optional

from figwatch.domain import Audit, AuditCompleted, AuditFailed, AuditResult
from figwatch.ports import CommentRepository, DesignDataRepository
from figwatch.processor import clean_reply

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditConfig:
    """Runtime configuration passed to skill execution — not domain state."""
    model: str
    claude_path: str
    reply_lang: str
    locale: str


class AuditService:
    """Coordinates audit execution using repositories and domain objects.

    Receives repository implementations via constructor (dependency injection).
    """

    def __init__(
        self,
        comment_repo: CommentRepository,
        design_repo: DesignDataRepository,
        config: AuditConfig,
        trigger_config: list,
    ):
        self._comments = comment_repo
        self._design = design_repo
        self._config = config
        self._trigger_config = trigger_config

    @property
    def config(self) -> AuditConfig:
        return self._config

    @property
    def design_repo(self) -> DesignDataRepository:
        return self._design

    def post_ack(self, audit: Audit, message: str) -> Optional[str]:
        try:
            return self._comments.post_reply(
                audit.comment.file_key,
                audit.reply_to_id,
                message,
            )
        except Exception as e:
            logger.warning('ack post failed (non-fatal)', extra={'error': str(e)})
            return None

    def delete_ack(self, audit: Audit, ack_id: Optional[str]) -> None:
        if not ack_id:
            return
        self._comments.delete_comment(audit.comment.file_key, ack_id)

    def update_ack(self, audit: Audit, old_ack_id: Optional[str], message: str) -> Optional[str]:
        self.delete_ack(audit, old_ack_id)
        return self.post_ack(audit, message)

    def post_reply(self, audit: Audit, message: str) -> None:
        self._comments.post_reply(
            audit.comment.file_key,
            audit.reply_to_id,
            message,
        )

    def execute(self, audit: Audit) -> str:
        """Run skill for an audit. Returns cleaned reply string.

        Calls audit.start_processing() / complete() / fail() to transition
        status and emit domain events. Raises on failure after calling fail().
        """
        from figwatch.skills import execute_skill

        audit.start_processing()

        try:
            response = execute_skill(
                audit,
                config=self._config,
                design_repo=self._design,
            )
            cleaned = clean_reply(response, self._trigger_config)
            audit.complete(AuditResult(reply_text=cleaned))
            return cleaned
        except Exception as err:
            audit.fail(str(err))
            raise

    def dispatch_events(self, audit: Audit, duration: float) -> None:
        """Collect and dispatch domain events for metrics/logging."""
        from figwatch.metrics import record_audit_completed

        events = audit.collect_events()
        for event in events:
            if isinstance(event, AuditCompleted):
                record_audit_completed(duration, 'success')
            elif isinstance(event, AuditFailed):
                record_audit_completed(duration, 'failed')
