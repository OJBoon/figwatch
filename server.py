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
  FIGWATCH_GEMINI_MODEL_ID    Override underlying Gemini model ID (optional)
  FIGWATCH_ANTHROPIC_MODEL_ID Override underlying Claude model ID (optional)
  FIGWATCH_FILES              Optional — comma-separated Figma file URLs or keys
  FIGWATCH_LOCALE             Locale for tone audits: uk, de, fr, nl, benelux (default: uk)
  FIGWATCH_PORT               Port to listen on (default: 8080)
  FIGWATCH_WORKERS            Number of worker threads (default: 4)
  FIGWATCH_GEMINI_RPM         Requests per minute for Gemini (default: 15; 0 disables)
  FIGWATCH_ANTHROPIC_RPM      Requests per minute for Anthropic (default: 5; 0 disables)
  FIGWATCH_LOG_LEVEL          Log level: DEBUG, INFO, WARNING, ERROR (default: INFO)
  FIGWATCH_LOG_FORMAT         Log format: text (default) or json
  FIGWATCH_SKILLS_DIR         Path to custom-skills directory (default: ./custom-skills)
  FIGWATCH_SKIP_TOKEN_CHECK   Skip Figma token validation at startup (for CI)
  FIGWATCH_FIGMA_PLAN        Figma plan: starter, professional, organization, enterprise
  FIGWATCH_FIGMA_SEAT        Figma seat type: dev, view (default: dev; ignored for starter)

  Observability (optional):
  OTEL_EXPORTER_OTLP_ENDPOINT   OTel collector endpoint (metrics disabled if unset)
"""

import hmac
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Allow running from the repo root without installing the package
_repo_root = os.path.dirname(os.path.abspath(__file__))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from figwatch.ack_updater import AckUpdater
from figwatch.domain import Audit, Comment, match_trigger
from figwatch.gateway import gateway_info
from figwatch.log_context import (
    clear_audit_context,
    new_audit_id,
    reset_audit_context,
    set_audit_context,
)
from figwatch.logging_config import configure_logging
from figwatch.messages import SIGN_OFF, queue_message
from figwatch.metrics import (
    init_metrics,
    record_token_expired,
    record_webhook_received,
    set_queue_depth_source,
)
from figwatch.providers.ai import CLAUDE_API_MODELS, GEMINI_MODELS, configure_limiters
from figwatch.providers.figma import (
    FigmaCommentRepository,
    FigmaDesignDataRepository,
    FigmaRateLimiter,
    FigmaTokenExpired,
    extract_file_key,
    figma_get_retry,
    validate_token,
)
from figwatch.queue_stats import InstrumentedQueue, QueuedItem
from figwatch.services import AuditConfig, AuditService
from figwatch.tracing import (
    attached_context,
    capture_context,
    format_trace_line,
    get_tracer,
    init_tracing,
    mark_error,
)
from figwatch.trigger_config import load_trigger_config
from figwatch.watcher import load_processed, save_processed

logger = logging.getLogger(__name__)

# Retry backoff schedule (seconds). Length determines the max backoff —
# subsequent retries reuse the final value.
_BACKOFFS = [30, 120, 300]


def _parse_file_keys(files_str):
    """Parse FIGWATCH_FILES — comma-separated Figma URLs or bare file keys."""
    keys = set()
    for item in filter(None, (i.strip() for i in files_str.split(','))):
        key = extract_file_key(item)
        if key:
            keys.add(key)
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

    user_info = comment.get('user') or payload.get('triggered_by') or {}
    user_handle = user_info.get('handle', 'unknown')

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
    """Execute via AuditService and swap the ack for the reply. Raises on failure."""
    logger.info('running skill', extra={'skill': audit.trigger_match.trigger.skill_ref})
    response = audit_service.execute_and_reply(audit, ack_id)
    logger.info('reply posted', extra={'reply_to': audit.reply_to_id, 'chars': len(response)})


def _comment_alive(audit_service, audit):
    try:
        return audit_service.comment_exists(audit)
    except Exception:
        logger.warning('comment_exists check failed — assuming alive')
        return True


def _schedule_retry(queued, ack_id, audit_service, work_queue, retry_timers):
    """Show the retry in the ack and requeue the item after the next backoff."""
    audit = queued.audit
    backoff = _BACKOFFS[min(queued.attempt - 1, len(_BACKOFFS) - 1)]
    queued.ack_id = audit_service.update_ack(
        audit, ack_id,
        (
            f'\u23f3 Something went wrong, trying again in {backoff}s '
            f'(attempt {queued.attempt})\u2026'
            f'{format_trace_line()}'
        ),
    )
    audit.collect_events()  # drain stale events before retry
    queued.attempt += 1

    timer = threading.Timer(backoff, work_queue.put, args=(queued,))
    timer.daemon = True
    timer.start()
    retry_timers[:] = [t for t in retry_timers if t.is_alive()] + [timer]


def _process(queued, span, work_queue, stop_event, audit_service, retry_timers):
    """One attempt at one audit: run it, then reply, retry, or give up."""
    audit = queued.audit
    trigger_kw = audit.trigger_match.trigger.keyword
    run_started_at = time.monotonic()
    logger.info(
        'queue.dequeued',
        extra={'depth': work_queue.stats().depth, 'waited': f'{queued.waited_seconds:.2f}s'},
    )
    ack_id = audit_service.update_ack(
        audit, queued.ack_id,
        f'\u23f3 Running {trigger_kw.lstrip("@")} audit\u2026{format_trace_line()}',
    )

    outcome, error = None, None
    try:
        _run_audit(audit, ack_id, audit_service)
        outcome = 'completed'
    except FigmaTokenExpired as err:
        record_token_expired()
        mark_error(span, err)
        logger.error('Figma token expired — cannot retry', extra={'attempt': queued.attempt})
        audit_service.reply_with_error(
            audit, ack_id,
            (
                f'Something went wrong and we are not able to fulfil your request.'
                f'{format_trace_line()}\n\n{SIGN_OFF}'
            ),
        )
        outcome, error = 'failed', err
    except Exception as err:
        span.record_exception(err)
        logger.warning('audit attempt failed', extra={'attempt': queued.attempt, 'error': str(err)})
        if not _comment_alive(audit_service, audit):
            logger.info('trigger comment deleted — abandoning audit')
            audit_service.delete_ack(audit, ack_id)
            outcome = 'abandoned'
        elif stop_event.is_set():
            logger.info('shutdown — not scheduling retry')
        else:
            _schedule_retry(queued, ack_id, audit_service, work_queue, retry_timers)

    if outcome is None:
        return
    total_seconds = time.monotonic() - queued.enqueued_at
    audit_service.dispatch_events(audit, total_seconds)
    if outcome == 'abandoned':
        return
    timings = {
        'queued': f'{queued.waited_seconds:.2f}s',
        'running': f'{time.monotonic() - run_started_at:.2f}s',
        'total': f'{total_seconds:.2f}s',
        'attempt': queued.attempt,
    }
    if error is None:
        logger.info('\u2705 audit.completed', extra=timings)
    else:
        logger.error('\u274c audit.failed', extra={**timings, 'last_error': str(error)})


def _worker_loop(work_queue: InstrumentedQueue, stop_event,
                 ack_updater: AckUpdater, audit_service: AuditService):
    retry_timers: list[threading.Timer] = []
    tracer = get_tracer()
    while not stop_event.is_set():
        queued = work_queue.get(timeout=1)
        if queued is None:
            continue

        # Stop the ack updater from touching this item's ack. Do this
        # BEFORE reading ack_id so we can't race with an in-flight update.
        ack_updater.cancel(queued.audit_id)

        audit = queued.audit
        # Restore trace context propagated from the webhook handler thread.
        with attached_context(queued.trace_context):
            token = set_audit_context(
                audit=queued.audit_id,
                trigger=audit.trigger_match.trigger.keyword,
                node=audit.comment.node_id,
                file=audit.comment.file_key,
                attempt=queued.attempt,
            )
            try:
                with tracer.start_as_current_span('audit', attributes={
                    'audit.id': queued.audit_id,
                    'audit.file_key': audit.comment.file_key,
                    'audit.node_id': audit.comment.node_id,
                    'audit.trigger': audit.trigger_match.trigger.keyword,
                }) as span:
                    _process(queued, span, work_queue, stop_event, audit_service, retry_timers)
            except Exception:
                logger.exception('worker crashed unexpectedly')
            finally:
                work_queue.task_done()
                reset_audit_context(token)


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
                ack_id = audit_service.post_ack(
                    audit, queue_message(trigger_kw, ahead, format_trace_line()),
                )

                queued = QueuedItem(
                    audit=audit,
                    ack_id=ack_id,
                    audit_id=audit_id,
                    trace_context=capture_context(),
                )
                work_queue.put(queued)

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


def _fail(message, **extra):
    logger.error(message, extra=extra)
    sys.exit(1)


def _env_int(name, default, *, min_value, max_value=None):
    """Integer env var; exits on a non-integer or out-of-range value (ADR-001)."""
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        _fail(f'invalid {name}', value=raw, expected='integer')
    if value < min_value or (max_value is not None and value > max_value):
        bounds = {'min': min_value} if max_value is None else {'min': min_value, 'max': max_value}
        _fail(f'invalid {name}', value=value, **bounds)
    return value


def _env_choice(name, default, choices):
    """Enum env var (case-insensitive); exits on a value outside `choices` (ADR-001)."""
    value = os.environ.get(name, default).strip().lower()
    if value not in choices:
        _fail(f'invalid {name}', value=value, valid=sorted(choices))
    return value


def main():
    configure_logging()

    pat = os.environ.get('FIGMA_PAT', '').strip()
    passcode = os.environ.get('FIGWATCH_WEBHOOK_PASSCODE', '').strip()

    if not pat:
        _fail('FIGMA_PAT is required')
    if not passcode:
        _fail('FIGWATCH_WEBHOOK_PASSCODE is required')

    if os.environ.get('FIGWATCH_SKIP_TOKEN_CHECK', '').strip().lower() in ('1', 'true', 'yes'):
        logger.warning('FIGWATCH_SKIP_TOKEN_CHECK set — skipping Figma token validation')
    else:
        try:
            handle = validate_token(pat)
            logger.info('figma token valid', extra={'user': handle})
        except FigmaTokenExpired:
            _fail(
                'Figma token expired — generate a new token at '
                'https://www.figma.com/developers/api#access-tokens'
            )
        except Exception as e:
            _fail('Figma token validation failed', error=str(e))

    files_str = os.environ.get('FIGWATCH_FILES', '').strip()
    allowed_file_keys = _parse_file_keys(files_str) if files_str else set()

    model = _env_choice('FIGWATCH_MODEL', 'gemini-flash', {*GEMINI_MODELS, *CLAUDE_API_MODELS})
    # Check the provider's credential now rather than failing on the first audit,
    # after the "queued" ack has already been posted.
    if model.startswith('gemini'):
        if not os.environ.get('GOOGLE_API_KEY', '').strip():
            _fail('GOOGLE_API_KEY is required for Gemini models', model=model)
    elif not (os.environ.get('ANTHROPIC_API_KEY', '').strip() or gateway_info()):
        _fail('ANTHROPIC_API_KEY is required for Claude models', model=model)

    locale = _env_choice('FIGWATCH_LOCALE', 'uk', {'uk', 'de', 'fr', 'nl', 'benelux'})
    port = _env_int('FIGWATCH_PORT', 8080, min_value=1, max_value=65535)
    worker_count = _env_int('FIGWATCH_WORKERS', 4, min_value=1)
    queue_update_rpm = _env_int('FIGWATCH_QUEUE_UPDATE_RPM', 5, min_value=1)
    configure_limiters(
        gemini_rpm=_env_int('FIGWATCH_GEMINI_RPM', 15, min_value=0),
        anthropic_rpm=_env_int('FIGWATCH_ANTHROPIC_RPM', 5, min_value=0),
    )
    figma_plan = _env_choice(
        'FIGWATCH_FIGMA_PLAN', 'professional',
        {'starter', 'professional', 'organization', 'enterprise'},
    )
    figma_seat = _env_choice('FIGWATCH_FIGMA_SEAT', 'dev', {'dev', 'view'})

    claude_path = 'api'

    figma_limiter = FigmaRateLimiter(plan=figma_plan, seat=figma_seat)

    skills_dir = os.environ.get('FIGWATCH_SKILLS_DIR', '').strip() or None
    if skills_dir and not os.path.isdir(skills_dir):
        _fail('FIGWATCH_SKILLS_DIR does not exist or is not a directory', path=skills_dir)

    init_metrics()
    init_tracing()

    trigger_config = load_trigger_config(skills_dir)
    triggers_str = ', '.join(t.get('trigger', '') for t in trigger_config)

    # Construct repositories and application service
    comment_repo = FigmaCommentRepository(pat, limiter=figma_limiter)
    design_repo = FigmaDesignDataRepository(pat, limiter=figma_limiter)
    audit_config = AuditConfig(
        model=model, claude_path=claude_path,
        reply_lang='en', locale=locale,
    )
    audit_service = AuditService(comment_repo, design_repo, audit_config, trigger_config)

    processed_ids = load_processed()
    processed_lock = threading.Lock()
    work_queue = InstrumentedQueue()
    set_queue_depth_source(lambda: work_queue.depth)
    stop_event = threading.Event()

    ack_updater = AckUpdater(work_queue, comment_repo, rate_per_minute=queue_update_rpm)
    ack_updater.start()

    logger.info(
        '\U0001f50d figwatch starting',
        extra={
            'port': port, 'workers': worker_count, 'model': model,
            'locale': locale,
            'queue_update_rpm': queue_update_rpm,
            'triggers': triggers_str,
            'files': ','.join(sorted(allowed_file_keys)) if allowed_file_keys else 'all',
        },
    )

    worker_threads = [
        threading.Thread(
            target=_worker_loop,
            args=(work_queue, stop_event,
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
    # Threaded: a webhook's node-id lookup and ack post are Figma round trips,
    # and must not hold up the next webhook.
    server = ThreadingHTTPServer(('', port), handler)
    server.daemon_threads = True

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
