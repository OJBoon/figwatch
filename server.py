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
"""

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

from figwatch.domain import WorkItem, load_trigger_config, match_trigger
from figwatch.log_context import (
    new_audit_id, set_audit_context, reset_audit_context, clear_audit_context,
)
from figwatch.logging_config import configure_logging
from figwatch.processor import (
    clean_reply, delete_ack, post_ack, post_reply, update_ack,
)
from figwatch.providers.figma import figma_get_retry
from figwatch.queue_stats import InstrumentedQueue, QueuedItem
from figwatch.skills import execute_skill
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


def _resolve_node_id(comment, file_key, pat, comment_id=None):
    """Return node_id for a comment, fetching the full comment from REST API if needed."""
    node_id = (comment.get('client_meta') or {}).get('node_id')
    if node_id:
        return node_id

    parent_id = comment.get('parent_id')
    lookup_id = parent_id or comment_id
    if not lookup_id:
        return None

    try:
        data = figma_get_retry(f'/files/{file_key}/comments', pat)
        for c in (data or {}).get('comments', []):
            if str(c.get('id')) == str(lookup_id):
                return (c.get('client_meta') or {}).get('node_id')
    except Exception as e:
        logger.warning('node_id lookup failed', extra={'error': str(e)})
    return None


def _build_work_item(payload, comment_id, pat, allowed_file_keys, locale, model, claude_path, trigger_config):
    """Parse a FILE_COMMENT payload into a WorkItem, or return (None, reason)."""
    file_key = payload.get('file_key')
    if allowed_file_keys and file_key not in allowed_file_keys:
        return None, 'file not in allowlist'

    comment = payload.get('comment') or {}
    message = comment.get('message') or comment.get('text', '')

    match = match_trigger(message, trigger_config)
    if not match:
        return None, 'no trigger'

    parent_id = comment.get('parent_id') or ''
    reply_to_id = parent_id or comment_id

    node_id = _resolve_node_id(comment, file_key, pat, comment_id=comment_id)
    if not node_id:
        return None, 'no node_id'

    item = WorkItem(
        file_key=file_key,
        comment_id=comment_id,
        reply_to_id=reply_to_id,
        node_id=node_id,
        trigger=match['trigger'],
        skill_path=match['skill'],
        user_handle=(comment.get('user') or payload.get('triggered_by') or {}).get('handle', 'unknown'),
        extra=match['extra'],
        locale=locale,
        model=model,
        reply_lang='en',
        pat=pat,
        claude_path=claude_path,
        on_status=None,
    )
    return item, None


# ── Worker loop ────────────────────────────────────────────────────────

def _run_audit(item, ack_id, trigger_config):
    """Execute the skill and post the reply. Raises on failure."""
    logger.info('running skill', extra={'skill': item.skill_path})
    response = execute_skill(item)
    logger.info('skill returned', extra={'chars': len(response)})

    delete_ack(item, ack_id)
    response = clean_reply(response, trigger_config)
    post_reply(item, response)
    logger.info('reply posted', extra={'reply_to': item.reply_to_id})


def _worker_loop(work_queue: InstrumentedQueue, stop_event, trigger_config, max_attempts):
    while not stop_event.is_set():
        queued = work_queue.get(timeout=1)
        if queued is None:
            # Either Queue.Empty (timed out) or poison pill.
            if stop_event.is_set():
                break
            continue

        item = queued.item
        ack_id = queued.ack_id
        run_started_at = time.monotonic()

        token = set_audit_context(
            audit=queued.audit_id,
            trigger=item.trigger,
            node=item.node_id,
            file=item.file_key,
            attempt=queued.attempt,
        )
        try:
            stats = work_queue.stats()
            logger.info(
                'queue.dequeued',
                extra={'depth': stats.depth, 'waited': f'{queued.waited_seconds:.2f}s'},
            )

            ack_id = update_ack(
                item, ack_id,
                f'\u23f3 Running {item.trigger.lstrip("@")} audit\u2026',
            )

            last_err = None
            success = False
            for attempt in range(max_attempts):
                if attempt > 0:
                    set_audit_context(attempt=attempt + 1)
                try:
                    _run_audit(item, ack_id, trigger_config)
                    ack_id = None
                    success = True
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
                    ack_id = update_ack(
                        item, ack_id,
                        (
                            f'\u23f3 {item.trigger.lstrip("@")} audit hit a snag '
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
                delete_ack(item, ack_id)
                try:
                    post_reply(
                        item,
                        (
                            f'Audit failed after {max_attempts} attempts.\n'
                            f'Last error: {last_err}\n\n{_EM_DASH} FigWatch'
                        ),
                    )
                except Exception:
                    logger.exception('error reply post failed')
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


# ── HTTP handler ───────────────────────────────────────────────────────

def _make_handler(pat, passcode, allowed_file_keys, locale, model, claude_path,
                  trigger_config, processed_ids, processed_lock, work_queue):
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

            if payload.get('passcode') != passcode:
                self._respond(403, 'Forbidden')
                return

            event_type = payload.get('event_type')

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

            item, reason = _build_work_item(
                payload, comment_id, pat, allowed_file_keys,
                locale, model, claude_path, trigger_config,
            )

            if item is None:
                with processed_lock:
                    processed_ids.discard(comment_id)
                logger.debug('skip', extra={'reason': reason})
                self._respond(200, reason)
                return

            save_processed(processed_ids)

            audit_id = new_audit_id()
            # Temporarily set context so the ack post + enqueue log lines
            # carry the new audit_id. Cleared on next request.
            set_audit_context(
                audit=audit_id,
                trigger=item.trigger,
                node=item.node_id,
                file=file_key,
            )

            logger.info(
                '\U0001f4ac trigger matched',
                extra={'user': item.user_handle},
            )

            ahead = work_queue.depth
            if ahead == 0:
                queue_msg = (
                    f'\u23f3 {item.trigger.lstrip("@")} audit queued '
                    f'\u2014 starting shortly\u2026'
                )
            else:
                queue_msg = (
                    f'\u23f3 {item.trigger.lstrip("@")} audit queued '
                    f'({ahead} ahead of you)\u2026'
                )
            ack_id = post_ack(item, queue_msg)

            queued = QueuedItem(
                item=item,
                ack_id=ack_id,
                audit_id=audit_id,
            )
            work_queue.put(queued)

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

    files_str = os.environ.get('FIGWATCH_FILES', '').strip()
    allowed_file_keys = _parse_file_keys(files_str) if files_str else set()

    locale = os.environ.get('FIGWATCH_LOCALE', 'uk')
    model = os.environ.get('FIGWATCH_MODEL', 'gemini-flash')
    port = int(os.environ.get('FIGWATCH_PORT', '8080'))
    worker_count = int(os.environ.get('FIGWATCH_WORKERS', '4'))
    max_attempts = int(os.environ.get('FIGWATCH_MAX_ATTEMPTS', '3'))
    claude_path = 'api'

    trigger_config = load_trigger_config()
    triggers_str = ', '.join(t.get('trigger', '') for t in trigger_config)

    processed_ids = load_processed()
    processed_lock = threading.Lock()
    work_queue = InstrumentedQueue()
    stop_event = threading.Event()

    logger.info(
        '\U0001f50d figwatch starting',
        extra={
            'port': port, 'workers': worker_count, 'model': model,
            'locale': locale, 'max_attempts': max_attempts,
            'triggers': triggers_str,
            'files': ','.join(sorted(allowed_file_keys)) if allowed_file_keys else 'all',
        },
    )

    worker_threads = [
        threading.Thread(
            target=_worker_loop,
            args=(work_queue, stop_event, trigger_config, max_attempts),
            name=f'figwatch-worker-{i}',
            daemon=True,
        )
        for i in range(worker_count)
    ]
    for t in worker_threads:
        t.start()

    handler = _make_handler(
        pat, passcode, allowed_file_keys,
        locale, model, claude_path,
        trigger_config, processed_ids, processed_lock, work_queue,
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
        for t in worker_threads:
            t.join(timeout=5)
        logger.info('\u23f9 all workers stopped — exiting')


if __name__ == '__main__':
    main()
