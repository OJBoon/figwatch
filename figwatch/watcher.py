"""FigWatch comment watcher — polls Figma for trigger comments and dispatches work items."""

import json
import math
import os
import queue
import re
import threading
import urllib.parse
import urllib.request
from collections import namedtuple
from pathlib import Path

from figwatch.handlers import STATUS_PROCESSING, STATUS_REPLIED, STATUS_ERROR

FIGMA_API = 'https://api.figma.com/v1'

# ── WorkItem ───────────────────────────────────────────────────────

WorkItem = namedtuple('WorkItem', [
    'file_key', 'comment_id', 'reply_to_id', 'node_id',
    'trigger', 'skill_path', 'user_handle', 'extra',
    'locale', 'model', 'reply_lang', 'pat', 'claude_path', 'on_status',
])

# ── Trigger config ─────────────────────────────────────────────────

DEFAULT_TRIGGERS = [
    {"trigger": "@tone", "skill": "builtin:tone"},
    {"trigger": "@ux", "skill": "builtin:ux"},
]


def _discover_custom_triggers():
    """Scan ./custom-skills/ for .md files and return trigger entries.

    Supports flat files (a11y.md → @a11y) and subdirectories (a11y/skill.md → @a11y).
    The directory is resolved relative to the process working directory, which is
    /app/custom-skills when running in Docker (WORKDIR /app).
    """
    custom_dir = Path(os.getcwd()) / 'custom-skills'
    if not custom_dir.is_dir():
        return []

    triggers = []
    seen = set()

    # Flat .md files: custom-skills/a11y.md → @a11y
    for path in sorted(custom_dir.glob('*.md')):
        name = path.stem
        if name not in seen:
            seen.add(name)
            triggers.append({'trigger': f'@{name}', 'skill': str(path.resolve())})

    # Subdirectory pattern: custom-skills/a11y/skill.md → @a11y
    for skill_dir in sorted(p for p in custom_dir.iterdir() if p.is_dir()):
        name = skill_dir.name
        if name in seen:
            continue
        for fname in ['skill.md', 'SKILL.md']:
            skill_path = skill_dir / fname
            if skill_path.exists():
                seen.add(name)
                triggers.append({'trigger': f'@{name}', 'skill': str(skill_path.resolve())})
                break

    return triggers


def load_trigger_config():
    """Load trigger config from config file or built-in defaults, plus any custom skills.

    Priority:
      1. ~/.figwatch/config.json  (written by the macOS app)
      2. Built-in defaults        (@tone, @ux)

    In both cases, any .md files found in ./custom-skills/ are appended automatically.
    """
    custom = _discover_custom_triggers()

    try:
        config_path = os.path.join(os.path.expanduser('~'), '.figwatch', 'config.json')
        with open(config_path) as f:
            config = json.load(f)
        triggers = config.get('triggers')
        if triggers and isinstance(triggers, list):
            existing = {t.get('trigger') for t in triggers}
            for t in custom:
                if t['trigger'] not in existing:
                    triggers.append(t)
            return triggers
    except Exception:
        pass

    return list(DEFAULT_TRIGGERS) + custom


def match_trigger(message, trigger_config):
    """Match a comment message against configured triggers.

    Returns {"trigger": str, "skill": str, "extra": str} or None.
    """
    lower = message.lower().strip()
    for entry in trigger_config:
        trigger = entry.get('trigger', '')
        if trigger and trigger.lower() in lower:
            idx = lower.index(trigger.lower())
            extra = message[idx + len(trigger):].strip()
            return {'trigger': trigger, 'skill': entry.get('skill', ''), 'extra': extra}
    return None


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

        dists = [(t, dist(t)) for t in all_texts]
        closest, closest_dist = min(dists, key=lambda x: x[1])

        if closest_dist < 200:
            dists.sort(key=lambda x: x[1])
            nearby = [t for t, _ in dists[:min(5, len(dists))]]
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

_processed_path_cache = None

def _processed_path():
    global _processed_path_cache
    if _processed_path_cache:
        return _processed_path_cache
    config_dir = os.path.join(os.path.expanduser('~'), '.figwatch')
    os.makedirs(config_dir, exist_ok=True)
    _processed_path_cache = os.path.join(config_dir, '.processed-comments.json')
    return _processed_path_cache


def load_processed():
    try:
        with open(_processed_path()) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_processed(ids):
    id_list = list(ids)
    if len(id_list) > 500:
        id_list = id_list[-500:]
        ids.clear()
        ids.update(id_list)
    with open(_processed_path(), 'w') as f:
        json.dump(id_list, f)


# ── Trigger detection (fast path — 1 API call) ─────────────────────

EM_DASH = '\u2014'
_OWN_REPLY_MARKERS = (EM_DASH + ' Claude', EM_DASH + ' Gemini')


def detect_triggers(file_key, pat, processed_ids, trigger_config, *, log, on_status=None):
    """Fetch comments, find trigger matches, return list[WorkItem].

    Fast path: single API call, <1s. Does NOT call Claude.
    """
    data = figma_get(f'/files/{file_key}/comments', pat)
    comments = data.get('comments', []) if data else []

    comment_map = {c['id']: c for c in comments}

    # Find threads we already replied to
    replied_to = set()
    for c in comments:
        if c.get('parent_id') and any(m in (c.get('message') or '') for m in _OWN_REPLY_MARKERS):
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
            if not any(m in (c.get('message') or '') for m in _OWN_REPLY_MARKERS):
                candidates.append(c)

    initial_count = len(processed_ids)
    items = []
    for comment in candidates:
        if comment['id'] in processed_ids:
            continue
        if comment['id'] in replied_to:
            continue
        if comment.get('parent_id') and comment['parent_id'] in replied_to:
            continue

        match = match_trigger(comment.get('message', ''), trigger_config)
        if not match:
            continue

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

        item = WorkItem(
            file_key=file_key,
            comment_id=comment['id'],
            reply_to_id=reply_to_id,
            node_id=node_id,
            trigger=match['trigger'],
            skill_path=match['skill'],
            user_handle=user_handle,
            extra=match['extra'],
            locale=None,      # filled by caller
            model=None,       # filled by caller
            reply_lang=None,  # filled by caller
            pat=pat,
            claude_path=None, # filled by caller
            on_status=on_status,
        )
        items.append(item)
        log(f'\U0001f4ac {match["trigger"]} comment by {user_handle} on node {node_id}')

    if len(processed_ids) > initial_count:
        save_processed(processed_ids)
    return items


# ── Work item processing (slow path — calls Claude) ────────────────

def process_work_item(item, *, log=print, trigger_config=None):
    """Process a single WorkItem: post ack, run handler, post reply.

    This is the slow path — runs on a worker thread.
    Returns True on success, False on failure (error reply posted to Figma).
    """
    from figwatch.handlers.generic import execute_skill

    file_key = item.file_key
    pat = item.pat
    reply_to_id = item.reply_to_id
    trigger = item.trigger

    if item.on_status:
        item.on_status(STATUS_PROCESSING, item)

    # Post acknowledgment
    ack_id = None
    try:
        ack = figma_post(f'/files/{file_key}/comments', {
            'message': f'\u23f3 {trigger.lstrip("@")} audit received \u2014 working on it\u2026',
            'comment_id': reply_to_id,
        }, pat)
        ack_id = ack.get('id')
        log(f'   ack posted (comment {ack_id})')
    except Exception as e:
        log(f'   ack failed (non-fatal): {e}')

    try:
        log(f'   running skill {item.skill_path}…')
        response = execute_skill(item)
        log(f'   skill returned {len(response)} chars')

        if ack_id:
            try:
                figma_delete(f'/files/{file_key}/comments/{ack_id}', pat)
                log(f'   ack deleted')
            except Exception as e:
                log(f'   ack delete failed (non-fatal): {e}')

        # Strip trigger words from the response to prevent feedback loops.
        for entry in (trigger_config or load_trigger_config()):
            trigger_word = entry.get('trigger', '')
            if trigger_word:
                response = re.sub(
                    r'(?<!\w)' + re.escape(trigger_word) + r'(?!\w)',
                    trigger_word.lstrip('@'),
                    response,
                    flags=re.IGNORECASE,
                )

        # Figma API comment limit is ~5000 chars
        FIGMA_COMMENT_LIMIT = 4900
        if len(response) > FIGMA_COMMENT_LIMIT:
            total = len(response)
            truncated = response[:FIGMA_COMMENT_LIMIT - 60]
            last_nl = truncated.rfind('\n')
            if last_nl > FIGMA_COMMENT_LIMIT // 2:
                truncated = truncated[:last_nl]
            response = truncated + f'\n\n(truncated \u2014 full audit was {total} chars)'

        figma_post(f'/files/{file_key}/comments', {
            'message': response,
            'comment_id': reply_to_id,
        }, pat)
        log(f'   reply posted to comment {reply_to_id}')

        if item.on_status:
            item.on_status(STATUS_REPLIED, item)
        return True

    except Exception as err:
        log(f'   failed: {err}')
        if ack_id:
            try:
                figma_delete(f'/files/{file_key}/comments/{ack_id}', pat)
            except Exception:
                pass
        try:
            figma_post(f'/files/{file_key}/comments', {
                'message': f'Audit failed: {err}\n\n{EM_DASH} FigWatch',
                'comment_id': reply_to_id,
            }, pat)
            log(f'   error reply posted')
        except Exception:
            pass
        if item.on_status:
            item.on_status(STATUS_ERROR, item, error=str(err))
        return False


# ── Watcher class ───────────────────────────────────────────────────

class FigmaWatcher:
    def __init__(self, file_key, pat, *, locale='uk', model='sonnet', reply_lang='en',
                 interval=30, claude_path='claude', log=print,
                 trigger_config=None, dispatch=None, on_poll=None, on_status=None,
                 initial_delay=0,
                 # Deprecated — use on_status instead
                 on_reply=None):
        self.file_key = file_key
        self.pat = pat
        self.locale = locale
        self.model = model
        self.reply_lang = reply_lang
        self.interval = interval
        self.claude_path = claude_path
        self.log = log
        self.trigger_config = trigger_config or load_trigger_config()
        self.dispatch = dispatch
        self.on_poll = on_poll
        self.on_status = on_status
        self.initial_delay = initial_delay
        self._on_reply = on_reply  # deprecated
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

    def reload_trigger_config(self, trigger_config):
        """Hot-reload triggers without restart."""
        self.trigger_config = trigger_config

    def _run(self):
        triggers_str = ', '.join(t.get('trigger', '') for t in self.trigger_config)
        self.log(f'\U0001f50d Watching {self.file_key} ({triggers_str})')

        if self.initial_delay > 0:
            self._stop_event.wait(timeout=self.initial_delay)

        while not self._stop_event.is_set():
            try:
                items = detect_triggers(
                    self.file_key, self.pat, self._processed,
                    self.trigger_config, log=self.log, on_status=self.on_status,
                )
                for item in items:
                    item = item._replace(
                        locale=self.locale,
                        model=self.model,
                        reply_lang=self.reply_lang,
                        claude_path=self.claude_path,
                        on_status=self.on_status,
                    )
                    if self.dispatch:
                        self.dispatch(item)
                    else:
                        process_work_item(item, trigger_config=self.trigger_config)
                        if self._on_reply:
                            self._on_reply(item.trigger, item.user_handle, item.node_id)

                if self.on_poll:
                    self.on_poll()
            except Exception as err:
                self.log(f'\u26a0\ufe0f Poll error: {err}')
            self._stop_event.wait(timeout=self.interval)


# ── CLI / server entry point ────────────────────────────────────────

if __name__ == '__main__':
    import sys

    args = sys.argv[1:]
    if not args or args[0] != 'watch' or len(args) < 2:
        print('Usage: python -m figwatch.watcher watch <file-key> [-l locale] [-i interval]')
        sys.exit(1)

    file_key = args[1]
    locale_idx = args.index('-l') if '-l' in args else -1
    locale = args[locale_idx + 1] if locale_idx >= 0 and locale_idx + 1 < len(args) else 'uk'
    interval_idx = args.index('-i') if '-i' in args else -1
    interval = int(args[interval_idx + 1]) if interval_idx >= 0 and interval_idx + 1 < len(args) else 30

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

    w = FigmaWatcher(file_key, pat, locale=locale, interval=interval)
    w.start()

    try:
        w._thread.join()
    except KeyboardInterrupt:
        w.stop()
