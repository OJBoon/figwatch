#!/usr/bin/env python3
"""FigWatch webhook server — receives Figma FILE_COMMENT webhooks and dispatches work items.

Before starting, register a webhook in Figma pointing at this server:

    curl -X POST https://api.figma.com/v2/webhooks \\
      -H "X-Figma-Token: $FIGMA_PAT" \\
      -H "Content-Type: application/json" \\
      -d '{
        "event_type": "FILE_COMMENT",
        "team_id": "<your-team-id>",
        "endpoint": "https://<your-host>/webhook",
        "passcode": "<FIGWATCH_WEBHOOK_PASSCODE>"
      }'

Environment variables:
  FIGMA_PAT                   Figma Personal Access Token (required)
  FIGWATCH_WEBHOOK_PASSCODE   Passcode set when registering the webhook (required)

  AI provider — set one:
  ANTHROPIC_API_KEY           Anthropic API key (for Claude models)
  GOOGLE_API_KEY              Google AI API key (for Gemini models)

  FIGWATCH_MODEL              Model to use (default: gemini-flash)
  FIGWATCH_FILES              Optional — comma-separated Figma file URLs or keys
  FIGWATCH_LOCALE             Locale for tone audits: uk, de, fr, nl, benelux (default: uk)
  FIGWATCH_PORT               Port to listen on (default: 8080)
  FIGWATCH_WORKERS            Number of worker threads (default: 4)
  FIGWATCH_MAX_ATTEMPTS       Retry attempts per audit before giving up (default: 3)
  FIGWATCH_GEMINI_RPM         Requests per minute for Gemini (default: 15; 0 disables)
  FIGWATCH_ANTHROPIC_RPM      Requests per minute for Anthropic (default: 5; 0 disables)
  FIGWATCH_LOG_LEVEL          Log level: DEBUG, INFO, WARNING, ERROR (default: INFO)
  FIGWATCH_LOG_FORMAT         Log format: text (default) or json
  FIGWATCH_SKILLS_DIR         Path to custom-skills directory (default: ./custom-skills)
  FIGWATCH_SKIP_TOKEN_CHECK   Skip Figma token validation at startup (for CI)
  FIGWATCH_FIGMA_PLAN           Figma plan: starter, professional, organization, enterprise (default: professional)
  FIGWATCH_FIGMA_SEAT           Figma seat type: dev, view (default: dev; ignored for starter)

  Observability (optional):
  OTEL_EXPORTER_OTLP_ENDPOINT   OTel collector endpoint (metrics disabled if unset)
"""

import hmac
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# Allow running from the repo root without installing the package
_repo_root = os.path.dirname(os.path.abspath(__file__))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from figwatch.ack_updater import AckUpdater
from figwatch.domain import Audit, Comment, match_trigger
from figwatch.trigger_config import load_trigger_config
from figwatch.log_context import (
    new_audit_id, set_audit_context, reset_audit_context, clear_audit_context,
)
from figwatch.logging_config import configure_logging
from figwatch.metrics import (
    init_metrics, record_queue_change, record_token_expired,
    record_webhook_received,
)
from figwatch.tracing import get_tracer, init_tracing
from figwatch.providers.ai import CLAUDE_API_MODELS, GEMINI_MODELS
from figwatch.providers.figma import (
    FigmaCommentRepository, FigmaDesignDataRepository, FigmaRateLimiter,
    FigmaTokenExpired, figma_get_retry, validate_token,
)
from figwatch.queue_stats import InstrumentedQueue, QueuedItem
from figwatch.services import AuditConfig, AuditService
from figwatch.watcher import load_processed, save_processed

logger = logging.getLogger(__name__)

# Retry backoff schedule (seconds). Length determines the max backoff —
# subsequent retries reuse the final value.
_BACKOFFS = [30, 120, 300]

_EM_DASH = '\u2014'


def _parse_file_keys(files_str):
    """Parse FIGWATCH_FILES — comma-separated Figma URLs or bare file keys."""
    keys = set()
    for item in files_str.split(','):
        item = item.strip()
        if not item:
            continue
        m = re.search(r'figma\.com/(?:design|file|board)/([a-zA-Z0-9]+)', item)
        if m:
            keys.add(m.group(1))
        elif re.match(r'^[a-zA-Z0-9]{10,}$', item):
            keys.add(item)
        else:
            logger.warning('skipping unrecognised FIGWATCH_FILES entry',
                           extra={'entry': item})
    return keys


def _resolve_node_id(comment, file_key, pat, comment_id=None, limiter=None):
    """Return node_id for a comment, fetching the full comment from REST API if needed."""
    node_id = (comment.get('client_meta') or {}).get('node_id')
    if node_id:
        return node_id

    parent_id = comment.get('parent_id')
    lookup_id = parent_id or comment_id
    if not lookup_id:
        return None

    try:
        data = figma_get_retry(f'/files/{file_key}/comments', pat, limiter=limiter)
        for c in (data or {}).get('comments', []):
            if str(c.get('id')) == str(lookup_id):
                return (c.get('client_meta') or {}).get('node_id')
    except Exception as e:
        logger.warning('node_id lookup failed', extra={'error': str(e)})
    return None


def _build_audit(payload, comment_id, pat, allowed_file_keys, trigger_config, audit_id,
                 limiter=None):
    """Parse a FILE_COMMENT payload into an Audit, or return (None, reason)."""
    file_key = payload.get('file_key')
    if allowed_file_keys and file_key not in allowed_file_keys:
        return None, 'file not in allowlist'

    comment = payload.get('comment') or {}
    message = comment.get('message') or comment.get('text', '')

    trigger_match = match_trigger(message, trigger_config)
    if not trigger_match:
        return None, 'no trigger'

    parent_id = comment.get('parent_id') or ''

    node_id = _resolve_node_id(comment, file_key, pat, comment_id=comment_id, limiter=limiter)
    if not node_id:
        return None, 'no node_id'

    user_handle = (comment.get('user') or payload.get('triggered_by') or {}).get('handle', 'unknown')

    audit = Audit(
        audit_id=audit_id,
        comment=Comment(
            comment_id=str(comment_id),
            message=message,
            parent_id=parent_id or None,
            node_id=node_id,
            user_handle=user_handle,
            file_key=file_key,
        ),
        trigger_match=trigger_match,
    )
    return audit, None


# ── Worker loop ────────────────────────────────────────────────────────

def _run_audit(audit, ack_id, audit_service):
    """Execute via AuditService. Raises on failure."""
    logger.info('running skill', extra={'skill': audit.trigger_match.trigger.skill_ref})
    response = audit_service.execute(audit)
    logger.info('skill returned', extra={'chars': len(response)})

    audit_service.delete_ack(audit, ack_id)
    audit_service.post_reply(audit, response)
    logger.info('reply posted', extra={'reply_to': audit.reply_to_id})


def _worker_loop(work_queue: InstrumentedQueue, stop_event,
                 max_attempts, ack_updater: AckUpdater, audit_service: AuditService):
    while not stop_event.is_set():
        queued = work_queue.get(timeout=1)
        if queued is None:
            if stop_event.is_set():
                break
            continue

        # Stop the ack updater from touching this item's ack. Do this
        # BEFORE reading ack_id so we can't race with an in-flight update.
        ack_updater.cancel(queued.audit_id)

        audit = queued.audit
        trigger_kw = audit.trigger_match.trigger.keyword
        ack_id = queued.ack_id
        run_started_at = time.monotonic()

        # Restore trace context propagated from the webhook handler thread.
        otel_token = None
        try:
            from opentelemetry import context as otel_context
            if queued.trace_context is not None:
                otel_token = otel_context.attach(queued.trace_context)
        except ImportError:
            pass

        token = set_audit_context(
            audit=queued.audit_id,
            trigger=trigger_kw,
            node=audit.comment.node_id,
            file=audit.comment.file_key,
            attempt=queued.attempt,
        )
        tracer = get_tracer()
        try:
          with tracer.start_as_current_span('audit', attributes={
              'audit.id': queued.audit_id,
              'audit.file_key': audit.comment.file_key,
              'audit.node_id': audit.comment.node_id,
              'audit.trigger': trigger_kw,
          }) as span:
            record_queue_change(-1)
            stats = work_queue.stats()
            logger.info(
                'queue.dequeued',
                extra={'depth': stats.depth, 'waited': f'{queued.waited_seconds:.2f}s'},
            )

            ack_id = audit_service.update_ack(
                audit, ack_id,
                f'\u23f3 Running {trigger_kw.lstrip("@")} audit\u2026',
            )

            last_err = None
            success = False
            attempt = 0
            for attempt in range(max_attempts):
                if attempt > 0:
                    set_audit_context(attempt=attempt + 1)
                try:
                    _run_audit(audit, ack_id, audit_service)
                    ack_id = None
                    success = True
                    break
                except FigmaTokenExpired as err:
                    last_err = err
                    record_token_expired()
                    span.record_exception(err)
                    logger.error(
                        'Figma token expired — skipping retries',
                        extra={'attempt': attempt + 1},
                    )
                    break
                except Exception as err:
                    last_err = err
                    logger.warning(
                        'audit attempt failed',
                        extra={'attempt': attempt + 1, 'max_attempts': max_attempts,
                               'error': str(err)},
                    )
                    if attempt >= max_attempts - 1:
                        break
                    backoff = _BACKOFFS[min(attempt, len(_BACKOFFS) - 1)]
                    ack_id = audit_service.update_ack(
                        audit, ack_id,
                        (
                            f'\u23f3 {trigger_kw.lstrip("@")} audit hit a snag '
                            f'({err}). Retrying in {backoff}s '
                            f'(attempt {attempt + 2}/{max_attempts})\u2026'
                        ),
                    )
                    if stop_event.wait(timeout=backoff):
                        logger.info('shutdown during backoff — aborting retry')
                        break

            running_seconds = time.monotonic() - run_started_at
            total_seconds = time.monotonic() - queued.enqueued_at

            if success:
                audit_service.dispatch_events(audit, total_seconds)
                logger.info(
                    '\u2705 audit.completed',
                    extra={
                        'queued': f'{queued.waited_seconds:.2f}s',
                        'running': f'{running_seconds:.2f}s',
                        'total': f'{total_seconds:.2f}s',
                        'attempts': attempt + 1,
                    },
                )
            else:
                try:
                    from opentelemetry.trace import StatusCode
                    span.set_status(StatusCode.ERROR, str(last_err))
                except ImportError:
                    pass
                audit_service.delete_ack(audit, ack_id)
                try:
                    audit_service.post_reply(
                        audit,
                        (
                            f'Audit failed after {max_attempts} attempts.\n'
                            f'Last error: {last_err}\n\n{_EM_DASH} FigWatch'
                        ),
                    )
                except Exception:
                    logger.exception('error reply post failed')
                audit_service.dispatch_events(audit, total_seconds)
                logger.error(
                    '\u274c audit.failed',
                    extra={
                        'queued': f'{queued.waited_seconds:.2f}s',
                        'running': f'{running_seconds:.2f}s',
                        'total': f'{total_seconds:.2f}s',
                        'attempts': max_attempts,
                        'last_error': str(last_err) if last_err else 'unknown',
                    },
                )
        except Exception:
            logger.exception('worker crashed unexpectedly')
        finally:
            work_queue.task_done()
            reset_audit_context(token)
            if otel_token is not None:
                try:
                    from opentelemetry import context as otel_context
                    otel_context.detach(otel_token)
                except ImportError:
                    pass


# ── HTTP handler ───────────────────────────────────────────────────────

def _make_handler(pat, passcode, allowed_file_keys,
                  trigger_config, processed_ids, processed_lock, work_queue,
                  ack_updater: AckUpdater,
                  audit_service: AuditService, limiter=None):
    class WebhookHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/health':
                self._respond(200, 'ok')
            else:
                self._respond(404, 'Not found')

        def do_POST(self):
            # Each request starts with a fresh context — worker threads will
            # re-set their own when they pick up the work item.
            clear_audit_context()

            if self.path != '/webhook':
                self._respond(404, 'Not found')
                return

            try:
                length = int(self.headers.get('Content-Length', 0))
                payload = json.loads(self.rfile.read(length))
            except Exception:
                self._respond(400, 'Bad request')
                return

            if not hmac.compare_digest(payload.get('passcode', ''), passcode):
                self._respond(403, 'Forbidden')
                return

            event_type = payload.get('event_type')

            record_webhook_received(event_type or 'UNKNOWN')

            if event_type == 'PING':
                logger.info('\U0001f3d3 ping received')
                self._respond(200, 'pong')
                return

            if event_type != 'FILE_COMMENT':
                self._respond(200, 'Ignored')
                return

            raw = payload.get('comment')
            payload['comment'] = (raw[0] if isinstance(raw, list) and raw else raw) or {}

            comment_id = payload.get('comment_id') or payload['comment'].get('id')
            file_key = payload.get('file_key', '?')

            logger.info(
                '\U0001f4e5 webhook received',
                extra={'file': file_key, 'comment': comment_id},
            )

            with processed_lock:
                if comment_id in processed_ids:
                    logger.debug('skip — already processed')
                    self._respond(200, 'Already processed')
                    return
                processed_ids.add(comment_id)

            tracer = get_tracer()
            with tracer.start_as_current_span('webhook.receive', attributes={
                'figma.file_key': file_key,
                'figma.comment_id': str(comment_id),
            }):
                audit_id = new_audit_id()
                audit, reason = _build_audit(
                    payload, comment_id, pat, allowed_file_keys,
                    trigger_config, audit_id, limiter=limiter,
                )

                if audit is None:
                    logger.debug('skip', extra={'reason': reason})
                    self._respond(200, reason)
                    return

                save_processed(processed_ids)

                trigger_kw = audit.trigger_match.trigger.keyword

                # Temporarily set context so the ack post + enqueue log lines
                # carry the new audit_id. Cleared on next request.
                set_audit_context(
                    audit=audit_id,
                    trigger=trigger_kw,
                    node=audit.comment.node_id,
                    file=file_key,
                )

                logger.info(
                    '\U0001f4ac trigger matched',
                    extra={'user': audit.comment.user_handle},
                )

                ahead = work_queue.depth
                if ahead == 0:
                    queue_msg = (
                        f'\u23f3 {trigger_kw.lstrip("@")} audit queued '
                        f'\u2014 starting shortly\u2026'
                    )
                else:
                    queue_msg = (
                        f'\u23f3 {trigger_kw.lstrip("@")} audit queued '
                        f'({ahead} ahead of you)\u2026'
                    )

                ack_id = audit_service.post_ack(audit, queue_msg)

                trace_ctx = None
                try:
                    from opentelemetry import context as otel_context
                    trace_ctx = otel_context.get_current()
                except ImportError:
                    pass

                queued = QueuedItem(
                    audit=audit,
                    ack_id=ack_id,
                    audit_id=audit_id,
                    trace_context=trace_ctx,
                )
                work_queue.put(queued)
                record_queue_change(1)

                # Record initial displayed position so the updater only fires
                # when the position actually changes.
                ack_updater.track_initial(audit_id, position=ahead)

                stats = work_queue.stats()
                logger.info('queue.enqueued', extra={'depth': stats.depth})

                self._respond(200, 'Queued')

        def _respond(self, code, message):
            body = message.encode()
            self.send_response(code)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            # Route BaseHTTPRequestHandler's own access logging through our logger.
            logger.debug(
                'http access',
                extra={'method': self.command, 'path': self.path,
                       'status': args[1] if len(args) > 1 else ''},
            )

    return WebhookHandler


def main():
    configure_logging()

    pat = os.environ.get('FIGMA_PAT', '').strip()
    passcode = os.environ.get('FIGWATCH_WEBHOOK_PASSCODE', '').strip()

    if not pat:
        logger.error('FIGMA_PAT is required')
        sys.exit(1)
    if not passcode:
        logger.error('FIGWATCH_WEBHOOK_PASSCODE is required')
        sys.exit(1)

    if os.environ.get('FIGWATCH_SKIP_TOKEN_CHECK', '').strip().lower() in ('1', 'true', 'yes'):
        logger.warning('FIGWATCH_SKIP_TOKEN_CHECK set — skipping Figma token validation')
    else:
        try:
            handle = validate_token(pat)
            logger.info('figma token valid', extra={'user': handle})
        except FigmaTokenExpired:
            logger.error(
                'Figma token expired — generate a new token at '
                'https://www.figma.com/developers/api#access-tokens'
            )
            sys.exit(1)
        except Exception as e:
            logger.error('Figma token validation failed', extra={'error': str(e)})
            sys.exit(1)

    files_str = os.environ.get('FIGWATCH_FILES', '').strip()
    allowed_file_keys = _parse_file_keys(files_str) if files_str else set()

    model = os.environ.get('FIGWATCH_MODEL', 'gemini-flash')
    valid_models = {*GEMINI_MODELS, *CLAUDE_API_MODELS}
    if model not in valid_models:
        logger.error('invalid FIGWATCH_MODEL',
                     extra={'value': model, 'valid': sorted(valid_models)})
        sys.exit(1)

    valid_locales = {'uk', 'de', 'fr', 'nl', 'benelux'}
    locale = os.environ.get('FIGWATCH_LOCALE', 'uk')
    if locale not in valid_locales:
        logger.error('invalid FIGWATCH_LOCALE',
                     extra={'value': locale, 'valid': sorted(valid_locales)})
        sys.exit(1)

    port = int(os.environ.get('FIGWATCH_PORT', '8080'))
    if port < 1 or port > 65535:
        logger.error('invalid FIGWATCH_PORT',
                     extra={'value': port, 'min': 1, 'max': 65535})
        sys.exit(1)

    worker_count = int(os.environ.get('FIGWATCH_WORKERS', '4'))
    if worker_count < 1:
        logger.error('invalid FIGWATCH_WORKERS',
                     extra={'value': worker_count, 'min': 1})
        sys.exit(1)

    max_attempts = int(os.environ.get('FIGWATCH_MAX_ATTEMPTS', '3'))
    if max_attempts < 1:
        logger.error('invalid FIGWATCH_MAX_ATTEMPTS',
                     extra={'value': max_attempts, 'min': 1})
        sys.exit(1)

    queue_update_rpm = int(os.environ.get('FIGWATCH_QUEUE_UPDATE_RPM', '5'))
    if queue_update_rpm < 1:
        logger.error('invalid FIGWATCH_QUEUE_UPDATE_RPM',
                     extra={'value': queue_update_rpm, 'min': 1})
        sys.exit(1)

    claude_path = 'api'

    gemini_rpm = int(os.environ.get('FIGWATCH_GEMINI_RPM', '15'))
    if gemini_rpm < 0:
        logger.error('invalid FIGWATCH_GEMINI_RPM',
                     extra={'value': gemini_rpm, 'min': 0})
        sys.exit(1)

    anthropic_rpm = int(os.environ.get('FIGWATCH_ANTHROPIC_RPM', '5'))
    if anthropic_rpm < 0:
        logger.error('invalid FIGWATCH_ANTHROPIC_RPM',
                     extra={'value': anthropic_rpm, 'min': 0})
        sys.exit(1)

    figma_plan = os.environ.get('FIGWATCH_FIGMA_PLAN', 'professional').strip().lower()
    valid_plans = ('starter', 'professional', 'organization', 'enterprise')
    if figma_plan not in valid_plans:
        logger.error('invalid FIGWATCH_FIGMA_PLAN',
                     extra={'value': figma_plan, 'valid': valid_plans})
        sys.exit(1)

    figma_seat = os.environ.get('FIGWATCH_FIGMA_SEAT', 'dev').strip().lower()
    valid_seats = ('dev', 'view')
    if figma_seat not in valid_seats:
        logger.error('invalid FIGWATCH_FIGMA_SEAT',
                     extra={'value': figma_seat, 'valid': valid_seats})
        sys.exit(1)

    figma_limiter = FigmaRateLimiter(plan=figma_plan, seat=figma_seat)

    skills_dir = os.environ.get('FIGWATCH_SKILLS_DIR', '').strip() or None
    if skills_dir and not os.path.isdir(skills_dir):
        logger.error('FIGWATCH_SKILLS_DIR does not exist or is not a directory',
                      extra={'path': skills_dir})
        sys.exit(1)

    init_metrics()
    init_tracing()

    trigger_config = load_trigger_config(skills_dir)
    triggers_str = ', '.join(t.get('trigger', '') for t in trigger_config)

    # Construct repositories and application service
    comment_repo = FigmaCommentRepository(pat)
    design_repo = FigmaDesignDataRepository(pat, limiter=figma_limiter)
    audit_config = AuditConfig(
        model=model, claude_path=claude_path,
        reply_lang='en', locale=locale,
    )
    audit_service = AuditService(comment_repo, design_repo, audit_config, trigger_config)

    processed_ids = load_processed()
    processed_lock = threading.Lock()
    work_queue = InstrumentedQueue()
    stop_event = threading.Event()

    ack_updater = AckUpdater(work_queue, comment_repo, rate_per_minute=queue_update_rpm)
    ack_updater.start()

    logger.info(
        '\U0001f50d figwatch starting',
        extra={
            'port': port, 'workers': worker_count, 'model': model,
            'locale': locale, 'max_attempts': max_attempts,
            'queue_update_rpm': queue_update_rpm,
            'triggers': triggers_str,
            'files': ','.join(sorted(allowed_file_keys)) if allowed_file_keys else 'all',
        },
    )

    worker_threads = [
        threading.Thread(
            target=_worker_loop,
            args=(work_queue, stop_event, max_attempts,
                  ack_updater, audit_service),
            name=f'figwatch-worker-{i}',
            daemon=True,
        )
        for i in range(worker_count)
    ]
    for t in worker_threads:
        t.start()

    handler = _make_handler(
        pat, passcode, allowed_file_keys,
        trigger_config, processed_ids, processed_lock, work_queue,
        ack_updater,
        audit_service, limiter=figma_limiter,
    )
    server = HTTPServer(('', port), handler)

    def _shutdown(sig, frame):
        logger.info('\u23f9 shutting down — draining in-flight audits')
        stop_event.set()
        # Workers wake via get(timeout=1) and break on stop_event — no pills needed.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    finally:
        ack_updater.stop()
        for t in worker_threads:
            t.join(timeout=5)
        logger.info('\u23f9 all workers stopped — exiting')


if __name__ == '__main__':
    main()
