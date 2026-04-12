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
                                Gemini:  gemini-flash, gemini-flash-lite,
                                         or any full Gemini model ID
                                Claude:  sonnet, opus, haiku
  FIGWATCH_FILES              Optional — comma-separated Figma file URLs or keys;
                              if set, only handle comments from these files
  FIGWATCH_LOCALE             Locale for tone audits: uk, de, fr, nl, benelux (default: uk)
  FIGWATCH_PORT               Port to listen on (default: 8080)
"""

import json
import os
import re
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

# Allow running from the repo root without installing the package
_repo_root = os.path.dirname(os.path.abspath(__file__))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from figwatch.handlers import figma_get_retry
from figwatch.watcher import (
    WorkItem, load_processed, load_trigger_config,
    match_trigger, process_work_item, save_processed,
)


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
            print(f'⚠️  Skipping unrecognised entry: {item!r}', flush=True)
    return keys


def _resolve_node_id(comment, file_key, pat, comment_id=None):
    """Return node_id for a comment, fetching the full comment from REST API if needed.

    Figma webhook v2 strips client_meta from the payload, so we always fall back
    to a REST API lookup using either the parent_id (for replies) or comment_id.
    """
    node_id = (comment.get('client_meta') or {}).get('node_id')
    if node_id:
        return node_id

    parent_id = comment.get('parent_id')
    lookup_id = parent_id or comment_id  # parent for replies, self for top-level
    if not lookup_id:
        return None

    try:
        data = figma_get_retry(f'/files/{file_key}/comments', pat)
        for c in (data or {}).get('comments', []):
            if str(c.get('id')) == str(lookup_id):
                return (c.get('client_meta') or {}).get('node_id')
    except Exception as e:
        print(f'   node_id lookup failed: {e}', flush=True)
    return None


def _build_work_item(payload, comment_id, pat, allowed_file_keys, locale, model, claude_path, trigger_config):
    """Parse a FILE_COMMENT payload into a WorkItem, or return (None, reason)."""
    file_key = payload.get('file_key')
    if allowed_file_keys and file_key not in allowed_file_keys:
        return None, 'file not in allowlist'

    comment = payload.get('comment') or {}
    # Webhook payloads use 'text'; REST API responses use 'message'
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


def _make_handler(pat, passcode, allowed_file_keys, locale, model, claude_path,
                  trigger_config, processed_ids, processed_lock, executor):
    class WebhookHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/health':
                self._respond(200, 'ok')
            else:
                self._respond(404, 'Not found')

        def do_POST(self):
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
                self._respond(200, 'pong')
                return

            if event_type != 'FILE_COMMENT':
                self._respond(200, 'Ignored')
                return

            # Figma sends comment as a list — normalise to a single object
            raw = payload.get('comment')
            payload['comment'] = (raw[0] if isinstance(raw, list) and raw else raw) or {}

            # Figma webhook protocol v2 puts the comment ID at the top level
            comment_id = payload.get('comment_id') or payload['comment'].get('id')
            file_key = payload.get('file_key', '?')
            print(f'📥 FILE_COMMENT {file_key} comment={comment_id}', flush=True)

            with processed_lock:
                if comment_id in processed_ids:
                    print(f'   skip: already processed', flush=True)
                    self._respond(200, 'Already processed')
                    return
                # Reserve immediately to prevent races on concurrent deliveries
                processed_ids.add(comment_id)

            item, reason = _build_work_item(
                payload, comment_id, pat, allowed_file_keys,
                locale, model, claude_path, trigger_config,
            )

            if item is None:
                with processed_lock:
                    processed_ids.discard(comment_id)
                print(f'   skip: {reason}', flush=True)
                self._respond(200, reason)
                return

            save_processed(processed_ids)
            print(f'💬 {item.trigger} by {item.user_handle} on {file_key}/{item.node_id}', flush=True)

            def _run(i):
                def _log(msg):
                    print(f'   {msg}', flush=True)
                ok = process_work_item(i, log=_log, trigger_config=trigger_config)
                if ok:
                    print(f'✅ {i.trigger} completed for {i.node_id}', flush=True)
                else:
                    print(f'❌ {i.trigger} failed for {i.node_id}', flush=True)

            executor.submit(_run, item)
            self._respond(200, 'Dispatched')

        def _respond(self, code, message):
            body = message.encode()
            self.send_response(code)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            print(f'→ {self.command} {self.path} {args[1] if len(args) > 1 else ""}', flush=True)

    return WebhookHandler


def main():
    pat = os.environ.get('FIGMA_PAT', '').strip()
    passcode = os.environ.get('FIGWATCH_WEBHOOK_PASSCODE', '').strip()

    if not pat:
        print('❌ FIGMA_PAT is required.', file=sys.stderr)
        sys.exit(1)
    if not passcode:
        print('❌ FIGWATCH_WEBHOOK_PASSCODE is required.', file=sys.stderr)
        sys.exit(1)

    files_str = os.environ.get('FIGWATCH_FILES', '').strip()
    allowed_file_keys = _parse_file_keys(files_str) if files_str else set()

    locale = os.environ.get('FIGWATCH_LOCALE', 'uk')
    model = os.environ.get('FIGWATCH_MODEL', 'gemini-flash')
    port = int(os.environ.get('FIGWATCH_PORT', '8080'))
    claude_path = 'api'  # server always uses REST API, not the Claude CLI

    trigger_config = load_trigger_config()
    triggers_str = ', '.join(t.get('trigger', '') for t in trigger_config)

    workers = int(os.environ.get('FIGWATCH_WORKERS', '4'))
    processed_ids = load_processed()
    processed_lock = threading.Lock()

    print('🔍 FigWatch webhook server starting', flush=True)
    print(f'   Listening: port {port}  →  POST /webhook', flush=True)
    print(f'   Triggers:  {triggers_str}', flush=True)
    print(f'   Locale:    {locale}  Model: {model}  Workers: {workers}', flush=True)
    if allowed_file_keys:
        print(f'   Files:     {", ".join(sorted(allowed_file_keys))}', flush=True)
    else:
        print('   Files:     all (no allowlist — set FIGWATCH_FILES to restrict)', flush=True)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        handler = _make_handler(
            pat, passcode, allowed_file_keys,
            locale, model, claude_path,
            trigger_config, processed_ids, processed_lock, executor,
        )
        server = HTTPServer(('', port), handler)

        def _shutdown(sig, frame):
            print('\n⏹  Shutting down…', flush=True)
            server.shutdown()
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        server.serve_forever()


if __name__ == '__main__':
    main()
