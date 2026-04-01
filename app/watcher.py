"""FigWatch comment watcher — polls Figma for @tone/@ux comments and responds."""

import json
import math
import os
import re
import threading
import urllib.parse
import urllib.request

from handlers.tone import tone_handler
from handlers.ux import ux_handler

FIGMA_API = 'https://api.figma.com/v1'

# ── Trigger routing ─────────────────────────────────────────────────

HANDLERS = {
    '@tone': {'handler': tone_handler, 'raw_mode': False},
    '@ux':   {'handler': ux_handler,   'raw_mode': True},
}


def match_handler(message):
    lower = message.lower().strip()
    for trigger, entry in HANDLERS.items():
        if trigger in lower:
            idx = lower.index(trigger)
            extra = message[idx + len(trigger):].strip()
            return {**entry, 'trigger': trigger, 'extra': extra}
    return None


def list_triggers():
    return list(HANDLERS.keys())


# ── Figma REST API ──────────────────────────────────────────────────

def _make_request(url, pat, method='GET', body=None):
    headers = {'X-Figma-Token': pat}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        if method == 'DELETE':
            return None
        return json.loads(r.read())


def figma_get(path, pat):
    return _make_request(f'{FIGMA_API}{path}', pat)


def figma_post(path, body, pat):
    return _make_request(f'{FIGMA_API}{path}', pat, method='POST', body=body)


def figma_delete(path, pat):
    _make_request(f'{FIGMA_API}{path}', pat, method='DELETE')


# ── Text extraction ─────────────────────────────────────────────────

def extract_text_from_node(node):
    texts = []

    def walk(n):
        if n.get('visible') is False:
            return
        if n.get('type') == 'TEXT' and (n.get('characters') or '').strip():
            box = n.get('absoluteBoundingBox') or {}
            texts.append({
                'name': n.get('name', ''),
                'text': n['characters'],
                'id': n.get('id', ''),
                'x': box.get('x', 0),
                'y': box.get('y', 0),
                'w': box.get('width', 0),
                'h': box.get('height', 0),
            })
        for child in n.get('children', []):
            walk(child)

    walk(node)
    return texts


def target_texts(node, all_texts, comment_meta):
    if node.get('type') == 'TEXT':
        return {'texts': all_texts, 'targeted': True, 'target_name': node.get('name'), 'primary_text': None}

    offset = (comment_meta or {}).get('node_offset')
    node_box = node.get('absoluteBoundingBox')
    if offset and node_box and len(all_texts) > 1:
        pin_x = node_box.get('x', 0) + (offset.get('x') or 0)
        pin_y = node_box.get('y', 0) + (offset.get('y') or 0)

        def dist(t):
            cx = t['x'] + t['w'] / 2
            cy = t['y'] + t['h'] / 2
            return math.sqrt((pin_x - cx) ** 2 + (pin_y - cy) ** 2)

        closest = min(all_texts, key=dist)
        closest_dist = dist(closest)

        if closest_dist < 200:
            sorted_texts = sorted(all_texts, key=dist)
            nearby = sorted_texts[:min(5, len(sorted_texts))]
            return {
                'texts': nearby, 'targeted': True,
                'target_name': closest['name'], 'primary_text': closest['text'],
            }

    return {'texts': all_texts, 'targeted': False, 'target_name': None, 'primary_text': None}


def detect_locale(extra, default):
    locales = ['de', 'fr', 'nl', 'benelux', 'uk']
    for word in (extra or '').lower().split():
        if word in locales:
            return word
    return default


# ── Processed comment tracking ──────────────────────────────────────

def _processed_path():
    config_dir = os.path.join(os.path.expanduser('~'), '.figwatch')
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, '.processed-comments.json')


def load_processed():
    try:
        with open(_processed_path()) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_processed(ids):
    # Prune to 500 most recent
    id_list = list(ids)
    if len(id_list) > 500:
        id_list = id_list[-500:]
        ids.clear()
        ids.update(id_list)
    with open(_processed_path(), 'w') as f:
        json.dump(id_list, f)


# ── Polling ─────────────────────────────────────────────────────────

EM_DASH = '\u2014'


def poll_once(file_key, pat, processed_ids, *, locale, claude_path, log, on_reply=None):
    data = figma_get(f'/files/{file_key}/comments', pat)
    comments = data.get('comments', []) if data else []

    # Build lookup
    comment_map = {c['id']: c for c in comments}

    # Find threads we already replied to
    replied_to = set()
    for c in comments:
        if c.get('parent_id') and EM_DASH + ' Claude' in (c.get('message') or ''):
            replied_to.add(c['parent_id'])
            processed_ids.add(c['parent_id'])

    # Filter candidates
    candidates = []
    for c in comments:
        if c['id'] in processed_ids or c.get('resolved_at'):
            continue
        if not c.get('parent_id'):
            if (c.get('client_meta') or {}).get('node_id'):
                candidates.append(c)
        else:
            if EM_DASH + ' Claude' not in (c.get('message') or ''):
                candidates.append(c)

    for comment in candidates:
        if comment['id'] in processed_ids:
            continue
        if comment['id'] in replied_to:
            continue
        if comment.get('parent_id') and comment['parent_id'] in replied_to:
            continue

        match = match_handler(comment.get('message', ''))
        if not match:
            continue

        trigger = match['trigger']
        handler = match['handler']
        raw_mode = match['raw_mode']
        extra = match['extra']

        node_id = (comment.get('client_meta') or {}).get('node_id')
        reply_to_id = comment['id']
        if comment.get('parent_id'):
            parent = comment_map.get(comment['parent_id'])
            node_id = node_id or ((parent.get('client_meta') or {}).get('node_id') if parent else None)
            reply_to_id = comment['parent_id']

        if not node_id:
            processed_ids.add(comment['id'])
            continue

        processed_ids.add(comment['id'])
        user_handle = comment.get('user', {}).get('handle', 'unknown')
        log(f'\U0001f4ac {trigger} comment by {user_handle} on node {node_id}')

        # Post acknowledgment
        ack_id = None
        try:
            ack = figma_post(f'/files/{file_key}/comments', {
                'message': f'\u23f3 {trigger} audit received \u2014 Claude is working on it\u2026',
                'comment_id': reply_to_id,
            }, pat)
            ack_id = ack.get('id')
        except Exception:
            pass

        try:
            if raw_mode:
                log(f'\U0001f4dd Running {trigger} audit...')
                response = handler(
                    node_id=node_id, file_key=file_key, pat=pat,
                    extra=extra, claude_path=claude_path,
                )
            else:
                enc_id = urllib.parse.quote(node_id, safe='')
                node_data = figma_get(f'/files/{file_key}/nodes?ids={enc_id}&depth=100', pat)
                node = (node_data or {}).get('nodes', {}).get(node_id, {}).get('document')

                if not node:
                    log(f'\u26a0\ufe0f Could not fetch node {node_id}, skipping')
                    continue

                all_texts = extract_text_from_node(node)
                if not all_texts:
                    if ack_id:
                        try: figma_delete(f'/files/{file_key}/comments/{ack_id}', pat)
                        except Exception: pass
                    figma_post(f'/files/{file_key}/comments', {
                        'message': f'No text nodes found here. Place the comment on or near a text layer.\n\n{EM_DASH} Claude',
                        'comment_id': reply_to_id,
                    }, pat)
                    continue

                targeting = target_texts(node, all_texts, comment.get('client_meta'))
                detected_locale = detect_locale(extra, locale)
                log(f'\U0001f4dd Running {trigger} audit...')

                response = handler(
                    texts=targeting['texts'], targeted=targeting['targeted'],
                    target_name=targeting['target_name'], primary_text=targeting['primary_text'],
                    locale=detected_locale, node_name=node.get('name', 'Unnamed frame'),
                    extra=extra, claude_path=claude_path,
                )

            # Delete ack, post response
            if ack_id:
                try: figma_delete(f'/files/{file_key}/comments/{ack_id}', pat)
                except Exception: pass

            figma_post(f'/files/{file_key}/comments', {
                'message': response,
                'comment_id': reply_to_id,
            }, pat)

            log(f'\u2705 Replied to comment {comment["id"]}')
            if on_reply:
                on_reply(trigger, comment.get('user', {}).get('handle', ''), node_id)

        except Exception as err:
            if ack_id:
                try: figma_delete(f'/files/{file_key}/comments/{ack_id}', pat)
                except Exception: pass
            log(f'\u274c Error processing comment {comment["id"]}: {err}')

    save_processed(processed_ids)


# ── Watcher class ───────────────────────────────────────────────────

class FigmaWatcher:
    def __init__(self, file_key, pat, *, locale='uk', interval=30,
                 claude_path='claude', log=print, on_reply=None):
        self.file_key = file_key
        self.pat = pat
        self.locale = locale
        self.interval = interval
        self.claude_path = claude_path
        self.log = log
        self.on_reply = on_reply
        self._stop_event = threading.Event()
        self._thread = None
        self._processed = load_processed()

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        self.log(f'\U0001f50d Watching {self.file_key} ({", ".join(list_triggers())})')
        while not self._stop_event.is_set():
            try:
                poll_once(
                    self.file_key, self.pat, self._processed,
                    locale=self.locale, claude_path=self.claude_path,
                    log=self.log, on_reply=self.on_reply,
                )
            except Exception as err:
                self.log(f'\u26a0\ufe0f Poll error: {err}')
            self._stop_event.wait(timeout=self.interval)


# ── CLI mode ────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    args = sys.argv[1:]
    if not args or args[0] != 'watch' or len(args) < 2:
        print('Usage: python watcher.py watch <file-key> [-l locale] [-i interval]')
        sys.exit(1)

    file_key = args[1]
    locale_idx = args.index('-l') if '-l' in args else -1
    locale = args[locale_idx + 1] if locale_idx >= 0 and locale_idx + 1 < len(args) else 'uk'
    interval_idx = args.index('-i') if '-i' in args else -1
    interval = int(args[interval_idx + 1]) if interval_idx >= 0 and interval_idx + 1 < len(args) else 30

    # Load PAT from config
    home = os.path.expanduser('~')
    pat = None
    for config_path in [
        os.path.join(home, '.figwatch', 'config.json'),
        os.path.join(home, '.figma-ds-cli', 'config.json'),
    ]:
        try:
            with open(config_path) as f:
                config = json.load(f)
                if config.get('figmaPat'):
                    pat = config['figmaPat']
                    break
        except Exception:
            pass

    if not pat:
        print('\u274c No Figma PAT found in ~/.figwatch/config.json')
        sys.exit(1)

    # Find claude
    claude_path = 'claude'
    for p in ['/opt/homebrew/bin/claude', '/usr/local/bin/claude']:
        if os.path.exists(p):
            claude_path = p
            break

    w = FigmaWatcher(file_key, pat, locale=locale, interval=interval, claude_path=claude_path)
    w.start()

    try:
        w._thread.join()
    except KeyboardInterrupt:
        w.stop()
